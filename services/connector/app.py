"""VoxArbor Node connector: the node dials out to a coordinator and serves its requests (docs/connect.md, protocol 1).

One WebSocket to COORDINATOR_URL; the coordinator's requests (the gateway contract: /stt /tts /translate /diarize
/fetch /capabilities) are multiplexed on it by stream id and executed against the local gateway. Without
COORDINATOR_URL the connector only answers /health. Stdlib + websockets + aiohttp."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import random
import re
import secrets
import ssl
import struct
import time
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web
import websockets
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

VERSION = os.environ.get("CONNECTOR_VERSION", "dev")
RELEASE = os.environ.get("VOXARBOR_NODE_RELEASE", "dev")  # baked into the image by CI
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "").strip().rstrip("/")
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8080")
LOCAL_TOKEN = os.environ.get("API_KEY", "")  # the node's own gateway token (NODE_TOKEN); never sent to the coordinator
JOIN_CODE = os.environ.get("NODE_JOIN_CODE", "").strip()
NODE_NAME = os.environ.get("NODE_NAME", "").strip() or os.uname().nodename
MAX_STREAMS = 1 if os.environ.get("NODE_MODE") == "gentle" else int(os.environ.get("CONNECT_MAX_STREAMS", "16"))  # gentle: one task at a time
INSECURE = os.environ.get("CONNECT_INSECURE_TLS", "") == "1"
STATE_DIR = os.environ.get("CONNECT_STATE_DIR", "/state")
STATE = os.path.join(STATE_DIR, "node.json")
ENROLL_STATE = os.path.join(STATE_DIR, "enroll.json")
CTL_DIR = os.environ.get("UPDATER_CTL", "/ctl")    # shared with node-updater (docs/updater.md); absent = no updater

PROTOCOL = 1
UA = f"voxarbor-node-connector/{VERSION}"
MAX_FRAME = 256 * 1024             # largest binary payload the protocol allows (what we accept)
CHUNK = 64 * 1024                  # what we send: smaller frames let pongs and close frames through sooner
UNSENT_LOWAT = 64 * 1024           # TCP_NOTSENT_LOWAT: at most this much unsent data sits in the kernel, so the
                                   # writer's priority (control frames first) also holds on a saturated uplink
TEXT_MAX = 1024 * 1024             # text frame limit
WINDOW = 1024 * 1024               # initial flow-control window per stream and direction
INLINE_MAX = 64 * 1024
BODY_MAX = 1024 ** 3               # request body (as the gateway)
IDLE_S = float(os.environ.get("CONNECT_IDLE_S", "600"))
CAPS_EVERY = 30.0
REQ_HEADERS = {"content-type", "accept", "last-event-id", "range", "x-request-id"}
RES_HEADERS = {"content-type", "content-length", "content-disposition", "retry-after"}
PATHS = ("/stt/", "/tts/", "/translate/", "/diarize/", "/fetch/", "/gen/")  # /gen: Remotion Render (0.18.0+)
VIEWER_PATHS = ("/stt/", "/tts/", "/translate/", "/diarize/", "/fetch/")
METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}

log = logging.getLogger("connector")
status = {"connected": False, "coordinator": None, "node_id": None, "last_error": None, "since": None,
          "streams": 0, "retry_at": None}



class _Perf:
    """speed from real work over the last hour: units done (audio seconds / sentences) per second of work"""
    def __init__(self, window: float = 3600):
        import collections
        self.window, self.items = window, collections.deque()

    def add(self, units: float, seconds: float) -> None:
        import time as _t
        if units > 0 and seconds > 0:
            self.items.append((_t.monotonic(), units, seconds))

    def summary(self) -> dict:
        import time as _t
        cut = _t.monotonic() - self.window
        while self.items and self.items[0][0] < cut:
            self.items.popleft()
        u, s = sum(x[1] for x in self.items), sum(x[2] for x in self.items)
        return {"jobs": len(self.items), "units": round(u, 1), "seconds": round(s, 1),
                "per_second": round(u / s, 2) if s else None, "window_s": int(self.window)}


STT_PERF = _Perf()

# ---------------------------------------------------------------- updater channel --------------------------------
def ctl_write(name: str, data: dict) -> None:
    if not os.path.isdir(CTL_DIR):
        return
    try:
        tmp = os.path.join(CTL_DIR, name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, os.path.join(CTL_DIR, name))
    except OSError as exc:
        log.warning("updater channel: %s", exc)


def on_update(u) -> None:
    """the coordinator's wish for this node's version (docs/updater.md): only these fields pass to the updater"""
    if not isinstance(u, dict):
        return
    v = u.get("version")
    ctl_write("desired.json", {"version": v if isinstance(v, str) and len(v) <= 32 else None,
                               "mode": u.get("mode") if u.get("mode") in ("auto", "notify", "off") else None,
                               "now": u.get("now") is True, "paused": u.get("paused") is True,
                               "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})


def updater_brief() -> dict | None:
    """state + change counter for every ping: the coordinator re-reads capabilities when seq changes"""
    try:
        with open(os.path.join(CTL_DIR, "status.json")) as f:
            st = json.load(f)
        return {"state": st.get("state"), "seq": st.get("seq")}
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- state ------------------------------------------
def load(path: str) -> dict | None:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def save(path: str, data: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def ws_url(base: str) -> str:
    u = urlsplit(base)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}[u.scheme]
    if scheme == "ws" and not (INSECURE or u.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("COORDINATOR_URL must be https:// (plain http only for localhost or CONNECT_INSECURE_TLS=1)")
    return urlunsplit((scheme, u.netloc, u.path.rstrip("/") + "/v1/nodes/connect", "", ""))


def coord_key(base: str) -> str:
    """the coordinator a token belongs to: scheme://host:port/path, lower-case host, no trailing slash"""
    u = urlsplit(base.strip())
    port = f":{u.port}" if u.port else ""
    return f"{u.scheme.lower()}://{(u.hostname or '').lower()}{port}{u.path.rstrip('/')}"


def node_state() -> dict | None:
    """this node's credentials for the current COORDINATOR_URL only (audit #5): after the URL changed the token of
    the previous coordinator is never sent to the new one; the node enrolls there with NODE_JOIN_CODE instead.
    A state from before 0.18.7 has no "coordinator": it is taken as this coordinator's and marked so."""
    st = load(STATE)
    if not st:
        return None
    if "coordinator" not in st:
        st["coordinator"] = coord_key(COORDINATOR_URL)
        save(STATE, st)
    return st if st["coordinator"] == coord_key(COORDINATOR_URL) else None


def ssl_context() -> ssl.SSLContext | None:
    ctx = ssl.create_default_context()
    if INSECURE:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _tune_socket(ws) -> None:
    """Keep the kernel's unsent backlog small (Linux TCP_NOTSENT_LOWAT): otherwise megabytes of audio frames wait
    in the socket buffer ahead of a pong or a close frame, and on a slow uplink the coordinator or a proxy gives up
    on the link (seen as a close without a close frame)."""
    import socket as _s

    try:
        sock = ws.transport.get_extra_info("socket")
        sock.setsockopt(_s.IPPROTO_TCP, getattr(_s, "TCP_NOTSENT_LOWAT", 25), UNSENT_LOWAT)
        sock.setsockopt(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)
    except (OSError, AttributeError) as exc:
        log.info("socket tuning skipped: %s", exc)


class Retry(Exception):
    def __init__(self, delay: float, reason: str, unauthorized: bool = False):
        super().__init__(reason)
        self.delay, self.reason, self.unauthorized = delay, reason, unauthorized


def code_sha(code: str) -> str:
    import hashlib

    return hashlib.sha256(code.encode()).hexdigest()


def fresh_code(state: dict | None) -> bool:
    """NODE_JOIN_CODE is set and is not the code this node already enrolled with (only its sha256 is kept)."""
    return bool(JOIN_CODE) and code_sha(JOIN_CODE) != (state or {}).get("join_code_sha256")


def retry_after(headers, default: float) -> float:
    try:
        return max(1.0, float(headers.get("Retry-After")))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- enrollment -------------------------------------
async def enroll(http: aiohttp.ClientSession, node_id: str | None = None) -> dict:
    """POST /v1/nodes/enroll; the result lives in /state/node.json. Again only with a new join code after the
    coordinator refused this node (4001 / 401), keeping its node_id so the coordinator keeps the same record."""
    st = load(ENROLL_STATE) or {}
    if st.get("code") != JOIN_CODE:
        st = {"code": JOIN_CODE, "request_id": secrets.token_hex(16)}
    node_id = node_id or st.get("node_id") or (node_state() or {}).get("node_id")
    if node_id:
        st["node_id"] = node_id
    save(ENROLL_STATE, st)
    body = {"code": JOIN_CODE, "request_id": st["request_id"], "name": NODE_NAME, "connector_version": VERSION}
    if node_id:
        body["node_id"] = node_id
    async with http.post(f"{COORDINATOR_URL}/v1/nodes/enroll", json=body, ssl=ssl_context()) as r:
        if r.status == 200:
            d = await r.json()
            save(STATE, {"node_id": d["node_id"], "node_token": d["node_token"], "enrolled": time.time(),
                         "join_code_sha256": code_sha(JOIN_CODE), "coordinator": coord_key(COORDINATOR_URL)})
            try:
                os.remove(ENROLL_STATE)
            except OSError:
                pass
            log.info("enrolled as node %s; NODE_JOIN_CODE can be removed from .env", d["node_id"])
            return load(STATE)
        if r.status in (401, 410):
            raise Retry(600, f"enroll refused ({r.status}): a new join code is needed")
        if r.status == 429:
            raise Retry(retry_after(r.headers, 60), "enroll rate limited (429)")
        raise Retry(30, f"enroll failed ({r.status})")


# ---------------------------------------------------------------- streams ----------------------------------------
class Stream:
    def __init__(self, sid: int, priority: int):
        self.id, self.priority = sid, priority
        self.body: asyncio.Queue = asyncio.Queue()   # request body chunks from the coordinator, None = end
        self.recv_allowed = WINDOW                     # bytes the coordinator may still send us
        self.recv_unacked = 0                          # consumed bytes not yet granted back
        self.send_credit = WINDOW                      # bytes we may still send the coordinator
        self.credit = asyncio.Event()
        self.credit.set()
        self.chunks: collections.deque = collections.deque()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.idle = False
        self.last = time.monotonic()
        self.viewer = False   # work for viewers (stt, tts, translate, diarize, fetch): what "busy" means for gen and
                              # the updater's drain; the coordinator's /gen jobs and /capabilities are not


class Link:
    """One WebSocket session: reader, prioritised round-robin writer, heartbeat, capabilities, request streams."""

    def __init__(self, ws, http: aiohttp.ClientSession, state: dict, heartbeat: float = 10):
        self.ws, self.http, self.state = ws, http, state
        self.heartbeat = heartbeat
        self.streams: dict[int, Stream] = {}
        self.max_streams = MAX_STREAMS
        self.control: collections.deque = collections.deque()
        self.wake = asyncio.Event()
        self.last_rx = time.monotonic()
        self.rr: collections.deque = collections.deque()  # stream ids in round-robin order
        self.out: dict[int, Stream] = {}                   # streams with frames still to send
        self.caps_hash = None
        self.caps_sent = 0.0
        self.caps_task: asyncio.Task | None = None

    # ---- outgoing
    def send_json(self, obj: dict) -> None:
        self.control.append(json.dumps(obj, ensure_ascii=False))
        self.wake.set()

    async def writer(self) -> None:
        while True:
            if self.control:
                await self.ws.send(self.control.popleft())
                continue
            st = self.next_stream()
            if st is None:
                self.wake.clear()
                if not self.control and self.next_stream(peek=True) is None:
                    await self.wake.wait()
                continue
            item = st.chunks.popleft()
            if not st.chunks and st.closed:
                self.out.pop(st.id, None)
            if isinstance(item, bytes):
                await self.ws.send(struct.pack(">I", st.id) + item)
            else:  # the stream's closing frame, queued after its data (res_end / res_error)
                await self.ws.send(item)

    def next_stream(self, peek: bool = False) -> Stream | None:
        """Round-robin over streams with queued data; viewer streams (lowest priority value) first."""
        ready = [x for x in self.out.values() if x.chunks]
        if not ready:
            return None
        if peek:
            return ready[0]
        best = min(x.priority for x in ready)
        for _ in range(len(self.rr)):
            i = self.rr[0]
            self.rr.rotate(-1)
            x = self.out.get(i)
            if x is not None and x.chunks and x.priority == best:
                return x
        return next(x for x in ready if x.priority == best)

    def queue_data(self, st: Stream, data: bytes) -> None:
        st.chunks.append(data)
        st.last = time.monotonic()
        self.out[st.id] = st
        self.wake.set()

    def close_stream(self, st: Stream, frame: dict) -> None:
        """Exactly one closing frame per stream, sent after its data."""
        if st.closed:
            return
        st.closed = True
        st.chunks.append(json.dumps(frame))
        self.out[st.id] = st
        self.streams.pop(st.id, None)
        status["streams"] = len(self.streams)
        self.wake.set()

    # ---- incoming
    async def reader(self) -> None:
        async for msg in self.ws:
            self.last_rx = time.monotonic()
            if isinstance(msg, bytes):
                self.on_data(msg)
                continue
            if len(msg) > TEXT_MAX:
                await self.ws.close(4009, "text frame too large")
                return
            try:
                f = json.loads(msg)
            except ValueError:
                continue
            t = f.get("type")
            if t == "req":
                self.on_req(f)
            elif t == "req_end":
                s = self.streams.get(f.get("id"))
                if s:
                    s.body.put_nowait(None)
            elif t == "cancel":
                s = self.streams.get(f.get("id"))
                if s and s.task:
                    s.task.cancel()
            elif t == "window":
                s = self.streams.get(f.get("id"))
                if s and isinstance(f.get("bytes"), int) and f["bytes"] > 0:
                    s.send_credit += f["bytes"]
                    s.credit.set()
            elif t == "ping":
                self.send_json({"type": "pong", "t": f.get("t")})
                if "update" in f:
                    on_update(f["update"])
            elif t == "update":
                on_update(f)
            elif t == "caps_get":
                await self.send_caps(force=True)
            elif t == "rotate_token" and isinstance(f.get("node_token"), str) and f["node_token"]:
                self.state = {**self.state, "node_token": f["node_token"], "rotated": time.time()}
                save(STATE, self.state)
                self.send_json({"type": "rotated"})
                log.info("node token rotated")

    def on_data(self, msg: bytes) -> None:
        if len(msg) < 4:
            return
        (sid,) = struct.unpack(">I", msg[:4])
        s = self.streams.get(sid)
        if s is None:
            return
        data = msg[4:]
        if len(data) > MAX_FRAME or len(data) > s.recv_allowed:
            self.fail(s, "protocol_error", "body sent past the flow-control window")
            return
        s.recv_allowed -= len(data)
        s.last = time.monotonic()
        s.body.put_nowait(data)

    def on_req(self, f: dict) -> None:
        sid = f.get("id")
        if not isinstance(sid, int) or sid in self.streams:
            return
        s = Stream(sid, int(f.get("priority", 1)) if str(f.get("priority", 1)).isdigit() else 1)
        self.streams[sid] = s
        self.rr.append(sid)
        status["streams"] = len(self.streams)
        method, path = str(f.get("method", "")).upper(), str(f.get("path", ""))
        s.viewer = path.startswith(VIEWER_PATHS)
        if len(self.streams) > self.max_streams:
            return self.fail(s, "too_many_streams", f"more than {self.max_streams} streams")
        if (updater_brief() or {}).get("state") == "draining":  # the node is about to update: send it elsewhere
            return self.fail(s, "draining", "the node is finishing its tasks before an update")
        if method not in METHODS or ".." in path or not (path.startswith(PATHS) or path.split("?")[0] == "/capabilities"):
            return self.fail(s, "forbidden_path", f"{method} {path[:80]} is not forwarded")
        if f.get("body") == "inline" and len(str(f.get("inline", "")).encode()) > INLINE_MAX:
            return self.fail(s, "bad_request", "inline body over 64 KiB")
        s.task = asyncio.create_task(self.run(s, f, method, path))

    def fail(self, s: Stream, code: str, detail: str) -> None:
        self.close_stream(s, {"type": "res_error", "id": s.id, "code": code, "detail": detail})

    # ---- one request against the local gateway
    async def request_body(self, s: Stream):
        total = 0
        while True:
            chunk = await s.body.get()
            if chunk is None:
                return
            total += len(chunk)
            if total > BODY_MAX:
                raise ValueError("request body over 1 GiB")
            s.recv_unacked += len(chunk)
            if s.recv_unacked >= CHUNK:  # grant the consumed bytes back
                s.recv_allowed += s.recv_unacked
                self.send_json({"type": "window", "id": s.id, "bytes": s.recv_unacked})
                s.recv_unacked = 0
            yield chunk

    async def run(self, s: Stream, f: dict, method: str, path: str) -> None:
        headers = {k: str(v) for k, v in (f.get("headers") or {}).items() if k.lower() in REQ_HEADERS}
        headers["Authorization"] = f"Bearer {LOCAL_TOKEN}"
        mode = f.get("body", "none")
        data = (str(f.get("inline", "")).encode() if mode == "inline" else
                self.request_body(s) if mode == "stream" else None)
        deadline = f.get("deadline_ms")
        try:
            coro = self.forward(s, method, path, headers, data)
            if isinstance(deadline, (int, float)) and deadline > 0:
                await asyncio.wait_for(coro, deadline / 1000)
            else:
                await coro
        except asyncio.TimeoutError:
            self.fail(s, "deadline", "deadline_ms passed")
        except asyncio.CancelledError:
            if s.idle:
                self.fail(s, "idle_timeout", f"no frames for {IDLE_S:.0f} s")
            else:
                self.fail(s, "cancelled", "cancelled")
        except aiohttp.ServerTimeoutError:
            self.fail(s, "upstream_timeout", "the local gateway did not answer in time")
        except (aiohttp.ClientError, OSError) as exc:
            self.fail(s, "upstream_unreachable", f"local gateway: {type(exc).__name__}")
        except ValueError as exc:
            self.fail(s, "bad_request", str(exc))
        except Exception as exc:  # noqa: BLE001  anything else must still close the stream (it would stay counted)
            log.warning("stream %s %s %s: %s", s.id, method, path[:60], exc)
            self.fail(s, "internal", type(exc).__name__)

    async def forward(self, s: Stream, method: str, path: str, headers: dict, data) -> None:
        stt = path.startswith("/stt/") and "transcriptions" in path
        t0, body = time.monotonic(), bytearray()
        async with self.http.request(method, GATEWAY + path, headers=headers, data=data, allow_redirects=False,
                                     timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=3600)) as r:
            waited = time.monotonic() - t0
            if waited > 10 and not path.startswith(("/tts/", "/stt/", "/diarize/")):  # those may think that long
                log.warning("slow upstream: %s %s answered %s after %.0f s", method, path[:80], r.status, waited)
            out = {k.lower(): v for k, v in r.headers.items()
                   if k.lower() in RES_HEADERS or k.lower().startswith("x-")}
            self.send_json({"type": "res", "id": s.id, "status": r.status, "headers": out})
            async for chunk in r.content.iter_any():
                if stt and r.status == 200 and len(body) < 4 * 2**20:
                    body += chunk
                for i in range(0, len(chunk), CHUNK):
                    part = chunk[i:i + CHUNK]
                    while s.send_credit < len(part):  # wait for the coordinator to grant more
                        s.credit.clear()
                        await s.credit.wait()
                    s.send_credit -= len(part)
                    self.queue_data(s, part)
        self.close_stream(s, {"type": "res_end", "id": s.id})
        if stt and body:
            try:  # verbose_json: "duration" = audio seconds (other formats: not measured)
                dur = float(json.loads(bytes(body)).get("duration") or 0)
                STT_PERF.add(dur, time.monotonic() - t0)
            except (ValueError, AttributeError, TypeError):
                pass

    # ---- periodic
    async def send_caps(self, force: bool = False) -> None:
        try:
            async with self.http.get(f"{GATEWAY}/capabilities", headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
                caps = await r.json()
        except Exception as exc:
            caps = {"schema": 1, "error": f"local capabilities: {type(exc).__name__}"}
        stable = json.dumps({k: v for k, v in caps.items() if k not in ("generated_at", "node")}, sort_keys=True)
        if force or stable != self.caps_hash or time.monotonic() - self.caps_sent >= CAPS_EVERY:
            self.caps_hash, self.caps_sent = stable, time.monotonic()
            self.send_json({"type": "caps", "capabilities": caps})

    async def ticker(self) -> None:
        last_ping = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - self.last_rx > 3 * self.heartbeat:
                queued = len(self.control) + sum(len(x.chunks) for x in self.out.values())
                qbytes = sum(len(c) for x in self.out.values() for c in x.chunks if isinstance(c, bytes))
                log.warning("heartbeat timeout: no frame from the coordinator for %.0f s (limit %.0f s); "
                            "own frames waiting to be sent: %d (%.1f MiB data), streams %d",
                            now - self.last_rx, 3 * self.heartbeat, queued, qbytes / 2**20, len(self.streams))
                await self.ws.close(4008, "heartbeat timeout")
                return
            if now - last_ping >= self.heartbeat:
                last_ping = now
                ping = {"type": "ping", "t": int(time.time() * 1000)}
                upd = updater_brief()
                if upd:
                    ping["updater"] = upd
                self.send_json(ping)
            for s in list(self.streams.values()):
                if now - s.last > IDLE_S and s.task and not s.idle:
                    s.idle = True
                    s.task.cancel()
            for s in list(self.streams.values()):  # a stream whose request task ended without closing it
                if s.task and s.task.done() and not s.closed:
                    self.fail(s, "internal", "request ended without a response")
            if int(now) % 5 == 0:
                # viewers: live requests of viewer work only (not /gen, not /capabilities, not ended tasks)
                viewers = sum(1 for s in self.streams.values() if s.viewer and s.task and not s.task.done())
                ctl_write("busy.json", {"streams": len(self.streams), "viewers": viewers, "at": time.time()})
            if int(now) % 5 == 0 and (self.caps_task is None or self.caps_task.done()):
                # its own task: a slow local /capabilities (up to 15 s) must not hold back the pings
                self.caps_task = asyncio.create_task(self.send_caps())

    async def serve(self) -> None:
        tasks = [asyncio.create_task(c) for c in (self.reader(), self.writer(), self.ticker())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    raise t.exception()
        finally:
            for t in tasks:
                t.cancel()
            for s in list(self.streams.values()):
                if s.task:
                    s.task.cancel()


# ---------------------------------------------------------------- main loop --------------------------------------
async def session(http: aiohttp.ClientSession, state: dict) -> None:
    url = ws_url(COORDINATOR_URL)
    headers = {"Authorization": f"Bearer {state['node_token']}", "X-VoxArbor-Node-Protocol": str(PROTOCOL),
               "User-Agent": UA}
    try:
        ws_ctx = connect(url, additional_headers=headers, ssl=ssl_context() if url.startswith("wss") else None,
                         max_size=TEXT_MAX + 64, ping_interval=None, open_timeout=20, close_timeout=5)
        async with ws_ctx as ws:
            _tune_socket(ws)
            link = Link(ws, http, state)
            caps = None
            try:
                async with http.get(f"{GATEWAY}/capabilities", headers={"Authorization": f"Bearer {LOCAL_TOKEN}"},
                                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                    caps = await r.json()
            except Exception:
                caps = {"schema": 1, "error": "local capabilities unavailable"}
            await ws.send(json.dumps({"type": "hello", "protocol": PROTOCOL, "node_name": NODE_NAME,
                                      "max_streams": MAX_STREAMS, "connector_version": VERSION,
                                      "voxarbor_node_version": caps.get("voxarbor_node_version", RELEASE),
                                      "capabilities": caps}, ensure_ascii=False))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), 20))
            if welcome.get("type") != "welcome":
                raise Retry(30, "no welcome from the coordinator")
            link.heartbeat = min(60, max(5, float(welcome.get("heartbeat_s", 10))))
            link.max_streams = min(MAX_STREAMS, int(welcome.get("max_streams", MAX_STREAMS)))
            if "update" in welcome:
                on_update(welcome["update"])
            status["welcomed"] = True  # the main loop resets its backoff: a short session is not a failed connect
            status.update(connected=True, coordinator=COORDINATOR_URL, node_id=welcome.get("node_id"),
                          since=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), last_error=None)
            log.info("connected to %s as node %s", COORDINATOR_URL, welcome.get("node_id"))
            await link.serve()
            state.update(link.state)  # a rotated token
    except InvalidStatus as exc:
        code, h = exc.response.status_code, exc.response.headers
        if code == 401:
            raise Retry(600, f"upgrade refused: {code}", unauthorized=True)
        if code == 426:  # protocol version not accepted: retry slowly, the coordinator may be mid-upgrade
            raise Retry(30, f"upgrade refused: {code}")
        raise Retry(retry_after(h, 0), f"upgrade refused: {code}")
    except ConnectionClosed as exc:
        # who closed: the coordinator's close frame (rcvd), ours (sent: e.g. our 4008 heartbeat timeout, even when
        # its answer never came back), or neither (the line dropped)
        if exc.rcvd:
            code, reason = exc.rcvd.code, f"by the coordinator: {exc.rcvd.reason}"
        elif exc.sent:
            code, reason = exc.sent.code, f"by us: {exc.sent.reason} (no close frame back)"
        else:
            code, reason = 1006, "connection lost (no close frame either way)"
        if code == 4001:
            raise Retry(600, f"closed {code}: {reason}", unauthorized=True)
        if code == 4002:  # protocol not supported: slow retry (30 s), not 10 min (a late hello once got 4002)
            raise Retry(30, f"closed {code}: {reason}")
        # 4004 (disabled by the admin) and everything else: the normal 1-60 s backoff
        if code == 4003:
            raise Retry(30, f"closed 4003: replaced by another connection of this node")
        raise Retry(0, f"closed {code}: {reason}")
    finally:
        status["connected"] = False


async def main_loop() -> None:
    if not COORDINATOR_URL:
        log.info("COORDINATOR_URL not set: idle")
        return
    try:  # audit #5: refuse a plain-http coordinator before any request (enroll would send the join code)
        ws_url(COORDINATOR_URL)
    except (ValueError, KeyError) as exc:
        log.error("%s: not connecting", exc if isinstance(exc, ValueError) else f"COORDINATOR_URL scheme {exc}")
        status.update(connected=False, last_error="COORDINATOR_URL must be https://")
        return
    backoff = 1.0
    # two clients: the tunnel passes the local gateway's bytes through untouched (auto_decompress=False); the
    # coordinator's own HTTP replies (enroll) are ordinary HTTP and may come compressed (a CDN gzips JSON)
    async with aiohttp.ClientSession(auto_decompress=False) as http, aiohttp.ClientSession(headers={"User-Agent": UA}) as coord:
        while True:
            started = time.monotonic()
            try:
                state = node_state()
                if state is None:
                    if not JOIN_CODE:
                        raise Retry(600, "not enrolled: set NODE_JOIN_CODE")
                    state = await enroll(coord)
                elif JOIN_CODE and not fresh_code(state):
                    log.info("already enrolled: NODE_JOIN_CODE is used up and can be removed from .env")
                try:
                    await session(http, state)
                except Retry as r:
                    if not (r.unauthorized and fresh_code(state)):
                        raise
                    # revoked or unknown token, and .env has a new join code: enroll again as the same node
                    log.warning("%s; enrolling again with the new NODE_JOIN_CODE as node %s", r.reason, state.get("node_id"))
                    await enroll(coord, node_id=state.get("node_id"))
                    continue
                delay, reason = 0.0, "closed"
            except Retry as r:
                delay, reason = r.delay, r.reason
            except (OSError, asyncio.TimeoutError, aiohttp.ClientError, websockets.exceptions.WebSocketException) as exc:
                delay, reason = 0.0, f"{type(exc).__name__}: {exc}"[:200]
            if status.pop("welcomed", False) or time.monotonic() - started > 60:
                backoff = 1.0  # the coordinator accepted us: reconnect quickly after a dropped link
            wait = delay or backoff
            backoff = min(60.0, backoff * 2)
            wait *= random.uniform(0.8, 1.2)
            status.update(last_error=reason, retry_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait)))
            log.warning("%s; retry in %.0f s", reason, wait)
            await asyncio.sleep(wait)


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": VERSION, "protocol": PROTOCOL, "enabled": bool(COORDINATOR_URL),
                              "stt_perf": STT_PERF.summary(),
                              "enrolled": os.path.isfile(STATE), "insecure_tls": INSECURE, **status})


SHA_RE = re.compile(r"^[0-9a-f]{64}$")


async def media(request: web.Request) -> web.StreamResponse:
    """Internal bridge for node services (gen): GET /media/{sha256} -> the coordinator's GET /v1/nodes/media/{sha256}
    with this node's token, streamed (Range passed through). The node token never leaves the connector; callers
    authenticate with the node's own gateway token. The coordinator serves only inputs of jobs given to this node."""
    if request.headers.get("Authorization") != f"Bearer {LOCAL_TOKEN}" or not LOCAL_TOKEN:
        return web.json_response({"detail": "invalid token"}, status=401)
    sha = request.match_info["sha"]
    st = node_state() or {}
    if not SHA_RE.fullmatch(sha):
        return web.json_response({"detail": "sha256 expected"}, status=400)
    if not COORDINATOR_URL or not st.get("node_token"):
        return web.json_response({"detail": "not connected to a coordinator"}, status=503)
    headers = {"Authorization": f"Bearer {st['node_token']}", "X-VoxArbor-Node-Protocol": str(PROTOCOL)}
    if request.headers.get("Range"):
        headers["Range"] = request.headers["Range"]
    http = request.app["http"]
    try:
        async with http.get(f"{COORDINATOR_URL}/v1/nodes/media/{sha}", headers=headers, ssl=ssl_context(),
                            timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=300)) as r:
            resp = web.StreamResponse(status=r.status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                if h in r.headers:
                    resp.headers[h] = r.headers[h]
            await resp.prepare(request)
            async for chunk in r.content.iter_chunked(1 << 20):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
        return web.json_response({"detail": f"coordinator: {type(exc).__name__}"}, status=502)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if INSECURE:
        log.warning("CONNECT_INSECURE_TLS=1: the coordinator's certificate is NOT verified (tests only)")
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/media/{sha}", media)
    app["http"] = aiohttp.ClientSession()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8000).start()
    await main_loop()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
