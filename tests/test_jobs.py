"""TTS jobs runner, no models (a fake synthesizer):
docker run --rm -e API_KEY=x -v $PWD/services/tts:/app:ro -v $PWD/tests/test_jobs.py:/t.py:ro ghcr.io/voxarbor/node-tts:<tag> python /t.py
Audit 2026-11-28: #32 a full disk fails one job, the runner takes the next; #45 a delete / cancel during the audio
assembly wins over "done", also across a restart."""
import asyncio
import os
import sys
import tempfile
import time

os.environ["TTS_JOBS_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, "/app")
import numpy as np  # noqa: E402
import jobs  # noqa: E402


async def synth(text, params):
    return np.full(2400, 0.1, dtype=np.float32).tobytes(), {}


def seg(t):
    return {"text": t, "spoken": t, "pause_after_ms": 100}


async def wait(J, jid, states=("done", "error", "canceled"), t=20):
    end = time.time() + t
    while time.time() < end:
        m = J.meta.get(jid)
        if m is None or m["status"] in states:
            return m
        await asyncio.sleep(0.05)
    raise AssertionError(f"{jid}: {J.meta.get(jid)}")


async def main():
    # ---- #32: the first job hits a full disk while writing a segment and again while saving its error status
    J = jobs.Jobs(synth, lambda pcm, fmt: b"audio")
    import soundfile as sf
    real_write, real_save = sf.write, J._save
    state = {"full": True}

    def full_write(*a, **k):
        if state["full"]:
            raise OSError(28, "No space left on device")
        return real_write(*a, **k)

    def full_save(jid, force=True):
        if state["full"] and J.meta.get(jid, {}).get("status") == "error":
            raise OSError(28, "No space left on device")
        return real_save(jid, force)

    sf.write, J._save = full_write, full_save
    J.start()
    a = await J.create("один", [seg("один")], {"engine": "holos", "response_format": "mp3"}, None)
    m = await wait(J, a["id"])
    assert m["status"] == "error", m
    state["full"] = False
    b = await J.create("два", [seg("два")], {"engine": "holos", "response_format": "mp3"}, None)
    m = await wait(J, b["id"])
    assert m["status"] == "done", f"the runner did not take the next job: {m}"
    print("#32 full disk: one job fails, the runner goes on ok")

    # ---- #45: delete and cancel while the audio is being assembled
    real_assemble = J._assemble

    def slow_assemble(jid):
        time.sleep(1.0)
        return real_assemble(jid)

    J._assemble = slow_assemble
    c = await J.create("три", [seg("три")], {"engine": "holos", "response_format": "mp3"}, None)
    await wait(J, c["id"], states=("running",))
    while not J.meta[c["id"]]["segments"][0]["done"]:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)          # now in _assemble
    await J.delete(c["id"])
    await asyncio.sleep(1.5)
    assert c["id"] not in J.meta and not os.path.exists(J._dir(c["id"])), J.meta.get(c["id"])
    d = await J.create("чотири", [seg("чотири")], {"engine": "holos", "response_format": "mp3"}, None)
    await wait(J, d["id"], states=("running",))
    while not J.meta[d["id"]]["segments"][0]["done"]:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    await J.cancel(d["id"])
    m = await wait(J, d["id"])
    assert m["status"] == "canceled", m
    print("#45 delete / cancel during assembly win ok")

    # ---- #45: a delete of a running job, then the service stops before the runner got there
    jid = "20990101-000000-abcdef"
    os.makedirs(J._dir(jid))
    J.meta[jid] = {"id": jid, "title": "x", "status": "running", "created": "", "updated": "", "chars": 1,
                   "params": {"engine": "holos"}, "duration_s": 0, "elapsed_s": 0, "error": None, "audio": None,
                   "ended": None, "segments": [seg("x") | {"done": False}]}
    await J.delete(jid)              # running: only marked
    J2 = jobs.Jobs(synth, lambda pcm, fmt: b"audio")
    J2.start()                       # the service starts again
    assert jid not in J2.meta and not os.path.exists(J2._dir(jid)), J2.meta.get(jid)
    print("#45 delete survives a restart ok")
    print("JOBS OK")


asyncio.run(main())
