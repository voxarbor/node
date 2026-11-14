"""Self-check of the worker-pool logic (same code in tts, translate, captcha-ocr). Run inside the translate image:
docker run --rm -e API_KEY=x -e IDLE_TTL=0 -e QUEUE_MAX=3 -v $PWD/tests/test_pool.py:/t.py:ro ghcr.io/voxarbor/node-translate:<tag> python /t.py"""
import asyncio, sys, time
sys.path.insert(0, "/app")
import app
from fastapi import HTTPException

_real_sleep = asyncio.sleep
app.asyncio.sleep = lambda s: _real_sleep(0.2)  # reaper ticks fast


class FakeRequest:
    def __init__(self, gone_after): self.t = time.monotonic() + gone_after
    async def is_disconnected(self): return time.monotonic() > self.t


async def main():
    reaper = asyncio.create_task(app._reaper())
    # 1. busy worker is never reaped (IDLE_TTL=0), and the loop keeps running meanwhile
    ticks = 0
    async def ticker():
        nonlocal ticks
        while True:
            await _real_sleep(0.1); ticks += 1
    tk = asyncio.create_task(ticker())
    await app._run(time.sleep, 3)
    assert ticks >= 25, f"event loop stalled: {ticks} ticks in 3 s"
    print("busy job survived the reaper, loop responsive:", ticks, "ticks")
    # 2. idle worker is reaped
    await _real_sleep(1.5)
    assert app._pool is None, "idle pool not reaped"
    print("idle pool reaped")
    # 3. queue limit -> 429
    jobs = [asyncio.create_task(app._run(time.sleep, 2)) for _ in range(4)]
    res = await asyncio.gather(*jobs, return_exceptions=True)
    codes = [getattr(r, "status_code", "ok") for r in res]
    assert codes.count(429) == 1, codes
    print("queue limit:", codes)
    # 4. waiting job of a disconnected client is dropped: a,b occupy the worker (+ its 1 pre-queued slot), c leaves
    t = time.monotonic()
    a = asyncio.create_task(app._run(time.sleep, 2))
    b = asyncio.create_task(app._run(time.sleep, 2))
    await _real_sleep(0.1)
    c = asyncio.create_task(app._run(time.sleep, 4, request=FakeRequest(0.5)))
    await _real_sleep(0.1)
    d = asyncio.create_task(app._run(time.sleep, 0))
    r = await asyncio.gather(a, b, c, d, return_exceptions=True)
    took = time.monotonic() - t
    assert getattr(r[2], "status_code", None) == 499, r
    assert took < 6, f"cancelled job still ran: {took:.1f}s"
    print(f"disconnected job dropped (a+b+d took {took:.1f}s, would be ~8s otherwise)")
    assert app._busy == 0
    tk.cancel(); reaper.cancel()
    print("ALL OK")

if __name__ == "__main__":
    asyncio.run(main())
