"""connector and COORDINATOR_URL (audit #5), fake coordinators, no models:
docker run --rm -v $PWD/services/connector/app.py:/app/app.py:ro -v $PWD/tests/test_connect_url.py:/t.py:ro \\
  ghcr.io/voxarbor/node-connector:<tag> python /t.py
- a plain-http coordinator (not localhost) gets no request at all: the join code is never sent in the clear
- after COORDINATOR_URL changed, the previous coordinator's token is not sent to the new one; it enrolls there"""
import asyncio
import json
import os
import subprocess
import sys

from aiohttp import web

hits: dict = {"open": [], "b": []}


async def record(name, r):
    hits[name].append((r.method, r.path, r.headers.get("Authorization")))
    if r.path == "/v1/nodes/enroll":
        return web.json_response({"node_id": "n-b", "node_token": "tok-b"})
    return web.Response(status=401)


def app(name):
    a = web.Application()
    a.router.add_route("*", "/{tail:.*}", lambda r: record(name, r))
    return a


async def serve(a, host, port):
    runner = web.AppRunner(a, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()


def connector(url, state, code=""):
    os.makedirs(state, exist_ok=True)
    env = {**os.environ, "COORDINATOR_URL": url, "GATEWAY_URL": "http://127.0.0.1:1", "API_KEY": "local",
           "NODE_JOIN_CODE": code, "CONNECT_STATE_DIR": state, "UPDATER_CTL": "/nonexistent"}
    return subprocess.Popen([sys.executable, "/app/app.py"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def main():
    await serve(app("open"), "127.0.0.2", 18091)   # plain http, not localhost by name or address
    await serve(app("b"), "127.0.0.1", 18092)      # the new coordinator

    p = connector("http://127.0.0.2:18091", "/tmp/s-open", code="join-1")
    await asyncio.sleep(4)
    p.kill()
    assert hits["open"] == [], hits["open"]
    print("plain http coordinator: no request, join code not sent ok")

    # enrolled at coordinator A, then .env points to B with a new join code
    os.makedirs("/tmp/s-move", exist_ok=True)
    json.dump({"node_id": "n-a", "node_token": "tok-a", "coordinator": "http://127.0.0.1:18090"},
              open("/tmp/s-move/node.json", "w"))
    p = connector("http://127.0.0.1:18092", "/tmp/s-move", code="join-b")
    for _ in range(40):
        await asyncio.sleep(0.25)
        if any(h[1] == "/v1/nodes/connect" for h in hits["b"]):
            break
    p.kill()
    assert all(h[2] != "Bearer tok-a" for h in hits["b"]), hits["b"]
    assert hits["b"] and hits["b"][0][1] == "/v1/nodes/enroll", hits["b"]
    assert json.load(open("/tmp/s-move/node.json"))["node_token"] == "tok-b"
    print("new coordinator: old token not sent, enrolled there ok")
    print("CONNECT URL OK")


asyncio.run(main())
