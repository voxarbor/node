"""connector while the updater drains (audit #16), no network:
docker run --rm -v $PWD/services/connector/app.py:/app/app.py:ro -v $PWD/tests/test_connect_drain.py:/t.py:ro \
  ghcr.io/voxarbor/node-connector:<tag> python /t.py
New work is refused; the coordinator still follows, collects and cancels /gen jobs."""
import asyncio
import sys

sys.path.insert(0, "/app")
import app  # noqa: E402

app.updater_brief = lambda: {"state": "draining"}


async def main():
    link = object.__new__(app.Link)
    link.streams, link.rr, link.max_streams = {}, [], 16
    refused, ran = [], []
    link.fail = lambda s, code, detail: refused.append((s.id, code))
    link.run = lambda s, f, method, path: ran.append((method, path)) or asyncio.sleep(0)
    sha = "a" * 64
    cases = [("POST", "/gen/v1/jobs", False), ("POST", "/tts/v1/audio/speech", False),
             ("GET", "/gen/v1/jobs/r-1-p-2", True), ("GET", f"/gen/v1/files/{sha}", True),
             ("DELETE", "/gen/v1/jobs/r-1-p-2", True), ("GET", "/capabilities", True),
             ("GET", "/stt/v1/models", False)]
    for i, (m, p, ok) in enumerate(cases, 1):
        link.on_req({"id": i, "method": m, "path": p})
        await asyncio.sleep(0)
        got = (m, p) in ran
        assert got == ok, f"{m} {p}: {'forwarded' if got else 'refused'} while draining ({refused})"
    print("draining: new work refused, /gen status, files and cancel forwarded ok")
    print("CONNECT DRAIN OK")


asyncio.run(main())
