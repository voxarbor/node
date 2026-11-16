"""Pong latency of the connector while its uplink is saturated by a big response (the GIGABAIT case: 1006 under
load). Fake gateway (a 40 MB stream) + fake coordinator (pings every second, measures the pong delay); the uplink is
throttled with tc on lo, so the container needs NET_ADMIN and iproute2:
docker run --rm --cap-add NET_ADMIN -v $PWD/services/connector/app.py:/app/app.py:ro \
  -v $PWD/tests/bench_slow_uplink.py:/t.py:ro <connector image> sh -c 'apt-get install -y iproute2 >/dev/null; python /t.py'
"""
import asyncio, json, os, struct, subprocess, sys, time

from aiohttp import WSMsgType, web

GW, CO = 18180, 18190
RATE = os.environ.get("RATE", "8mbit")


async def gw_big(r):
    resp = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
    await resp.prepare(r)
    chunk = os.urandom(256 * 1024)
    for _ in range(160):  # 40 MB
        await resp.write(chunk)
    await resp.write_eof()
    return resp


async def gw_caps(r):
    return web.json_response({"schema": 1, "voice_core_version": "vX", "services": {}})


delays: list[float] = []
done = asyncio.Event()


async def connect(r):
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
    await ws.prepare(r)
    await ws.receive()  # hello
    await ws.send_str(json.dumps({"type": "welcome", "protocol": 1, "node_id": "n", "heartbeat_s": 60, "max_streams": 4}))
    await ws.send_str(json.dumps({"type": "req", "id": 1, "method": "GET", "path": "/tts/v1/big", "headers": {}, "body": "none"}))
    sent = {}

    async def pinger():
        n = 0
        while not done.is_set():
            n += 1
            sent[n] = time.monotonic()
            await ws.send_str(json.dumps({"type": "ping", "t": n}))
            await asyncio.sleep(1)

    task = asyncio.create_task(pinger())
    got = 0
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            got += len(msg.data) - 4
            await ws.send_str(json.dumps({"type": "window", "id": 1, "bytes": len(msg.data) - 4}))
            continue
        f = json.loads(msg.data)
        if f["type"] == "pong" and f.get("t") in sent:
            delays.append(time.monotonic() - sent.pop(f["t"]))
        if f["type"] in ("res_end", "res_error"):
            break
    done.set()
    task.cancel()
    print(f"received {got / 2**20:.0f} MiB", flush=True)
    return ws


async def main():
    gw = web.Application(); gw.router.add_get("/tts/v1/big", gw_big); gw.router.add_get("/capabilities", gw_caps)
    co = web.Application(); co.router.add_get("/v1/nodes/connect", connect)
    for app, port in ((gw, GW), (co, CO)):
        runner = web.AppRunner(app, access_log=None); await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
    # throttle only traffic to the coordinator port (the node's uplink), not the gateway
    for cmd in (f"tc qdisc add dev lo root handle 1: prio",
                f"tc qdisc add dev lo parent 1:3 handle 30: tbf rate {RATE} burst 32kbit latency 400ms",
                f"tc filter add dev lo protocol ip parent 1:0 prio 3 u32 match ip dport {CO} 0xffff flowid 1:3"):
        subprocess.run(cmd.split(), check=True)
    os.makedirs("/tmp/st", exist_ok=True)
    json.dump({"node_id": "n", "node_token": "t"}, open("/tmp/st/node.json", "w"))
    env = {**os.environ, "COORDINATOR_URL": f"http://127.0.0.1:{CO}", "GATEWAY_URL": f"http://127.0.0.1:{GW}",
           "API_KEY": "x", "CONNECT_STATE_DIR": "/tmp/st"}
    proc = subprocess.Popen([sys.executable, "/app/app.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    await asyncio.wait_for(done.wait(), 600)
    proc.terminate()
    d = sorted(delays)
    print(f"uplink {RATE}: transfer {time.monotonic() - t0:.0f} s, pongs {len(d)}, "
          f"median {d[len(d) // 2]:.2f} s, p95 {d[int(len(d) * 0.95)]:.2f} s, max {d[-1]:.2f} s")


asyncio.run(main())
