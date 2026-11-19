"""Self-check of the connector (docs/connect.md) against a fake coordinator and a fake gateway, no models:
docker run --rm -v $PWD/services/connector/app.py:/app/app.py:ro -v $PWD/tests/test_connect.py:/t.py:ro \
  ghcr.io/voxarbor/node-connector:<tag> python /t.py"""

import asyncio
import json
import os
import struct
import subprocess
import sys
import time

from aiohttp import WSMsgType, web

GW, CO = 18080, 18090
events: dict = {"enroll": [], "hello": None, "auth": [], "frames": []}


# ---------------------------------------------------------------- fake gateway -----------------------------------
async def gw_caps(r):
    assert r.headers.get("Authorization") == "Bearer local", r.headers.get("Authorization")
    return web.json_response({"schema": 1, "voxarbor_node_version": "v9.9.9", "services": {}, "generated_at": str(time.time())})


async def gw_echo(r):
    assert r.headers.get("Authorization") == "Bearer local"
    body = await r.read()
    return web.Response(body=body, headers={"X-RTF": "0.1", "Content-Type": r.headers.get("Content-Type", "x/y"),
                                            "Set-Cookie": "no=1"})


async def gw_upload(r):
    n = 0
    async for chunk in r.content.iter_any():
        n += len(chunk)
    return web.json_response({"bytes": n})


async def gw_big(r):
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await resp.prepare(r)
    for _ in range(12):
        await resp.write(b"x" * 256 * 1024)
    await resp.write_eof()
    return resp


async def gw_slow(r):
    await asyncio.sleep(30)
    return web.Response(text="late")


async def gw_sse(r):
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(r)
    for i in range(3):
        await resp.write(f"data: {i}\n\n".encode())
        await asyncio.sleep(0.2)
    await resp.write_eof()
    return resp


# ---------------------------------------------------------------- fake coordinator -------------------------------
class Coord:
    def __init__(self):
        self.ws = None
        self.connected = asyncio.Event()
        self.res: dict[int, dict] = {}
        self.data: dict[int, bytearray] = {}
        self.done: dict[int, asyncio.Event] = {}
        self.ctrl: list = []
        self.order: list = []
        self.window_from_node: dict[int, int] = {}
        self.grant = True
        self.revoked: set = set()

    async def enroll(self, r):
        d = await r.json()
        events["enroll"].append(d)
        if d["code"] == "join-2":  # a second code after a revocation keeps the node's record when it sends node_id
            return web.json_response({"node_id": d.get("node_id") or "n-new", "node_token": "tok-3"})
        if d["code"] != "join-ok" or events["enroll"][:-1]:  # single-use
            return web.json_response({"detail": "bad code"}, status=401)
        import gzip  # replies gzip-compressed, as a CDN in front of the coordinator does
        return web.Response(body=gzip.compress(json.dumps({"node_id": "n-1", "node_token": "tok-1"}).encode()),
                            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})

    async def connect(self, r):
        events["auth"].append((r.headers.get("Authorization"), r.headers.get("X-VoxArbor-Node-Protocol")))
        if r.headers.get("Authorization") not in ("Bearer tok-1", "Bearer tok-2", "Bearer tok-3") \
                or r.headers.get("Authorization") in self.revoked:
            return web.Response(status=401)
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await ws.prepare(r)
        hello = json.loads((await ws.receive()).data)
        events["hello"] = hello
        await ws.send_str(json.dumps({"type": "welcome", "protocol": 1, "node_id": "n-1", "heartbeat_s": 5,
                                      "max_streams": 4}))
        self.ws = ws
        self.connected.set()
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                (sid,) = struct.unpack(">I", msg.data[:4])
                self.data.setdefault(sid, bytearray()).extend(msg.data[4:])
                self.order.append(sid)
                if self.grant:
                    await ws.send_str(json.dumps({"type": "window", "id": sid, "bytes": len(msg.data) - 4}))
                continue
            f = json.loads(msg.data)
            events["frames"].append(f["type"])
            if f["type"] == "res":
                self.res[f["id"]] = f
            elif f["type"] in ("res_end", "res_error"):
                self.res.setdefault(f["id"], {})["end"] = f
                self.done.setdefault(f["id"], asyncio.Event()).set()
            elif f["type"] == "window":
                self.window_from_node[f["id"]] = self.window_from_node.get(f["id"], 0) + f["bytes"]
            elif f["type"] == "ping":
                await ws.send_str(json.dumps({"type": "pong", "t": f["t"]}))
            else:
                self.ctrl.append(f)
        self.connected.clear()
        return ws

    async def req(self, sid, method, path, body=None, stream=None, **extra):
        self.done[sid] = asyncio.Event()
        f = {"type": "req", "id": sid, "method": method, "path": path,
             "headers": {"content-type": "application/json", "authorization": "Bearer evil", "cookie": "x"},
             "body": "stream" if stream is not None else "inline" if body is not None else "none", **extra}
        if body is not None:
            f["inline"] = body
        await self.ws.send_str(json.dumps(f))
        if stream is not None:
            sent, credit = 0, 1024 * 1024
            for i in range(0, len(stream), 256 * 1024):
                part = stream[i:i + 256 * 1024]
                while sent + len(part) > credit:  # respect the node's window
                    await asyncio.sleep(0.01)
                    credit = 1024 * 1024 + self.window_from_node.get(sid, 0)
                await self.ws.send_bytes(struct.pack(">I", sid) + part)
                sent += len(part)
            await self.ws.send_str(json.dumps({"type": "req_end", "id": sid}))

    async def wait(self, sid, t=20):
        await asyncio.wait_for(self.done[sid].wait(), t)
        return self.res[sid], bytes(self.data.get(sid, b""))


async def main():
    gw = web.Application()
    gw.router.add_get("/capabilities", gw_caps)
    gw.router.add_post("/tts/v1/echo", gw_echo)
    gw.router.add_post("/diarize/v1/upload", gw_upload)
    gw.router.add_get("/stt/v1/big", gw_big)
    gw.router.add_get("/tts/v1/slow", gw_slow)
    gw.router.add_get("/tts/v1/sse", gw_sse)
    co = Coord()
    ca = web.Application()
    ca.router.add_post("/v1/nodes/enroll", co.enroll)
    ca.router.add_get("/v1/nodes/connect", co.connect)
    for app, port in ((gw, GW), (ca, CO)):
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()

    state = "/tmp/cstate"
    os.makedirs(state, exist_ok=True)
    env = {**os.environ, "COORDINATOR_URL": f"http://127.0.0.1:{CO}", "GATEWAY_URL": f"http://127.0.0.1:{GW}",
           "API_KEY": "local", "NODE_JOIN_CODE": "join-ok", "NODE_NAME": "test-node", "CONNECT_STATE_DIR": state,
           "CONNECT_MAX_STREAMS": "8"}
    proc = subprocess.Popen([sys.executable, "/app/app.py"], env=env)
    try:
        await asyncio.wait_for(co.connected.wait(), 30)
        e = events["enroll"][0]
        assert e["code"] == "join-ok" and len(e["request_id"]) == 32 and e["name"] == "test-node", e
        assert json.load(open(f"{state}/node.json"))["node_token"] == "tok-1"
        assert oct(os.stat(f"{state}/node.json").st_mode)[-3:] == "600"
        assert events["auth"][0] == ("Bearer tok-1", "1"), events["auth"]
        h = events["hello"]
        assert h["protocol"] == 1 and h["node_name"] == "test-node" and h["max_streams"] == 8, h
        assert h["capabilities"]["voxarbor_node_version"] == "v9.9.9" and h["voxarbor_node_version"] == "v9.9.9"
        print("enroll + hello ok")

        await co.req(1, "POST", "/tts/v1/echo", body='{"a": 1}')
        res, data = await co.wait(1)
        assert res["status"] == 200 and data == b'{"a": 1}', (res, data)
        assert res["headers"].get("x-rtf") == "0.1" and "set-cookie" not in res["headers"], res["headers"]
        assert res["end"]["type"] == "res_end"
        print("inline request ok (local auth replaced the coordinator's, header allowlists)")

        blob = os.urandom(5 * 1024 * 1024 + 123)
        await co.req(2, "POST", "/diarize/v1/upload", stream=blob)
        res, data = await co.wait(2, 60)
        assert json.loads(data)["bytes"] == len(blob), data
        assert co.window_from_node.get(2, 0) >= len(blob) - 1024 * 1024, co.window_from_node
        print("5 MB streamed upload with flow control ok")

        await co.req(3, "GET", "/stt/v1/big")
        res, data = await co.wait(3, 60)
        assert len(data) == 12 * 256 * 1024, len(data)
        print("3 MB streamed response ok")

        await co.req(4, "GET", "/etc/passwd")
        res, _ = await co.wait(4)
        assert res["end"]["code"] == "forbidden_path", res
        await co.req(5, "GET", "/tts/../capabilities")
        res, _ = await co.wait(5)
        assert res["end"]["code"] == "forbidden_path", res
        print("path allowlist ok")

        await co.req(6, "GET", "/tts/v1/slow", deadline_ms=500)
        res, _ = await co.wait(6)
        assert res["end"]["code"] == "deadline", res
        await co.req(7, "GET", "/tts/v1/slow")
        await asyncio.sleep(0.5)
        await co.ws.send_str(json.dumps({"type": "cancel", "id": 7}))
        res, _ = await co.wait(7)
        assert res["end"]["code"] == "cancelled", res
        print("deadline + cancel ok (each ends with one frame)")

        await co.req(8, "GET", "/tts/v1/sse")
        res, data = await co.wait(8)
        assert data == b"data: 0\n\ndata: 1\n\ndata: 2\n\n", data
        print("SSE passthrough ok")

        for i in range(10, 15):
            await co.req(i, "GET", "/tts/v1/slow")
        await asyncio.sleep(0.5)
        res = co.res.get(14, {})
        assert res.get("end", {}).get("code") == "too_many_streams", co.res
        for i in range(10, 14):
            await co.ws.send_str(json.dumps({"type": "cancel", "id": i}))
        for i in range(10, 14):
            await co.wait(i)
        print("max_streams ok")

        # round-robin: two big responses interleave, the viewer one (priority 0) first
        co.order.clear()
        await co.req(20, "GET", "/stt/v1/big", priority=1)
        await co.req(21, "GET", "/stt/v1/big", priority=0)
        await co.wait(20, 60)
        await co.wait(21, 60)
        o = co.order
        n21 = o.count(21)
        assert o.index(21) < len(o) // 4, o[:24]  # the viewer stream starts early
        half = o[: len(o) // 2]
        assert half.count(21) >= n21 // 3, (half.count(21), n21)  # and interleaves instead of waiting behind 20
        print("round-robin + priority ok:", "".join("v" if x == 21 else "b" for x in o[:24]))

        await co.ws.send_str(json.dumps({"type": "caps_get"}))
        await asyncio.sleep(1)
        assert any(f["type"] == "caps" for f in co.ctrl), co.ctrl
        await co.ws.send_str(json.dumps({"type": "rotate_token", "node_token": "tok-2"}))
        await asyncio.sleep(1)
        assert any(f["type"] == "rotated" for f in co.ctrl), co.ctrl
        assert json.load(open(f"{state}/node.json"))["node_token"] == "tok-2"
        await co.ws.close(code=1012, message=b"restart")
        await asyncio.wait_for(co.connected.wait(), 30)
        assert events["auth"][-1] == ("Bearer tok-2", "1"), events["auth"]
        assert len(events["enroll"]) == 1, "enrolled again"
        print("caps_get, token rotation, reconnect with the new token, no re-enroll ok")

        # dropped links (a proxy cuts the WebSocket, 1006) after short sessions: every reconnect stays fast
        for _ in range(5):
            await asyncio.wait_for(co.connected.wait(), 30)
            co.connected.clear()
            t0 = time.monotonic()
            co.ws._req.transport.abort() if getattr(co.ws, "_req", None) else await co.ws.close(code=1011)
            await asyncio.wait_for(co.connected.wait(), 6)
            assert time.monotonic() - t0 < 6
        print("5 dropped short sessions, each reconnect < 6 s ok")

        # revocation: 4001 with the used code in .env -> no new enrollment; restart with a new code -> enroll again
        # as the same node (node_id from /state/node.json), then connect with the new token
        assert "join_code_sha256" in json.load(open(f"{state}/node.json"))
        co.revoked |= {"Bearer tok-1", "Bearer tok-2"}
        co.connected.clear()
        await co.ws.close(code=4001, message=b"revoked")
        await asyncio.sleep(3)
        assert len(events["enroll"]) == 1 and not co.connected.is_set(), events["enroll"]
        proc.terminate()
        proc.wait()
        env["NODE_JOIN_CODE"] = "join-2"
        proc = subprocess.Popen([sys.executable, "/app/app.py"], env=env)
        await asyncio.wait_for(co.connected.wait(), 30)
        e = events["enroll"][-1]
        assert e["code"] == "join-2" and e.get("node_id") == "n-1", e
        assert events["auth"][-1] == ("Bearer tok-3", "1"), events["auth"][-3:]
        st = json.load(open(f"{state}/node.json"))
        assert st["node_id"] == "n-1" and st["node_token"] == "tok-3", st
        print("revocation: no re-enroll with the used code; new code -> same node_id, new token ok")
        print("ALL OK")
    finally:
        proc.terminate()


asyncio.run(main())
