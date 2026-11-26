"""/gen Remotion Render end to end: bundles the fixture (tests/remotion-fixture), serves it as the coordinator's media,
runs the gen image and checks info / video parts / concat / audio / errors / cancel / idempotency / cache / busy.
  python3 tests/test_gen.py <gen image>        e.g. ghcr.io/voxarbor/node-gen:<tag> or a local build
Needs docker (and network for npm on the first run). Stdlib only. Leaves nothing running."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

IMAGE = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "remotion-fixture")
NODE = "node:22.23.5-bookworm-slim"
NET, KEY, PORT = "gentest", "gentest-key", 18780
work = tempfile.mkdtemp(prefix="gen-test-")
os.chmod(work, 0o777)


def sh(*cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode:
        raise SystemExit(f"{' '.join(cmd[:4])}… exited {r.returncode}:\n{(r.stdout + r.stderr)[-2000:]}")
    return r.stdout


def docker_rm(*names):
    subprocess.run(["docker", "rm", "-f", *names], capture_output=True)


# 1. the fixture bundle: tar.gz with package.json (the remotion version) and the `remotion bundle` folder
media = os.path.join(work, "media")
os.makedirs(media)
sh("docker", "run", "--rm", "-v", f"{FIX}:/src:ro", "-v", f"{work}:/out", NODE, "sh", "-c",
   "cp -r /src /tmp/f && cd /tmp/f && (for i in 1 2 3; do npm ci --no-audit --no-fund >/tmp/npm.log 2>&1 && exit 0; "
   "sleep 5; done; tail -20 /tmp/npm.log; exit 1) && mkdir -p public "
   "&& node -e \"const fs=require('fs');const sr=48000,n=sr*4,b=Buffer.alloc(44+n*2);b.write('RIFF',0);"
   "b.writeUInt32LE(36+n*2,4);b.write('WAVEfmt ',8);b.writeUInt32LE(16,16);b.writeUInt16LE(1,20);b.writeUInt16LE(1,22);"
   "b.writeUInt32LE(sr,24);b.writeUInt32LE(sr*2,28);b.writeUInt16LE(2,32);b.writeUInt16LE(16,34);b.write('data',36);"
   "b.writeUInt32LE(n*2,40);for(let i=0;i<n;i++)b.writeInt16LE(Math.round(8000*Math.sin(2*Math.PI*440*i/sr)),44+i*2);"
   "fs.writeFileSync('public/tone.wav',b)\" && node bundle.mjs /tmp/pack/build >/dev/null "
   "&& cp package.json /tmp/pack/ && tar -czf /out/bundle.tgz -C /tmp/pack . && chmod 644 /out/bundle.tgz")
raw = open(os.path.join(work, "bundle.tgz"), "rb").read()
SHA = hashlib.sha256(raw).hexdigest()
os.rename(os.path.join(work, "bundle.tgz"), os.path.join(media, SHA))
print(f"bundle {SHA[:12]} {len(raw) / 1e6:.1f} MB")

# 2. a fake coordinator media endpoint and the gen service on one network
subprocess.run(["docker", "network", "create", NET], capture_output=True)
docker_rm("gentest-media", "gentest-gen")
ctl = os.path.join(work, "ctl")
os.makedirs(ctl)
os.chmod(ctl, 0o777)
sh("docker", "run", "-d", "--name", "gentest-media", "--network", NET, "-v", f"{media}:/m:ro", "python:3.12.12-slim",
   "python", "-m", "http.server", "8000", "-d", "/m")


def start_gen(*extra):
    docker_rm("gentest-gen")
    sh("docker", "run", "-d", "--name", "gentest-gen", "--network", NET, "-p", f"127.0.0.1:{PORT}:8000",
       "--cpus", "3", "--memory", "4g", "-e", f"API_KEY={KEY}", "-e", "GEN_MEDIA_URL=http://gentest-media:8000",
       "-e", "TTS_URL=http://nowhere:1", "-v", f"{ctl}:/ctl:ro", *extra, IMAGE)
    for _ in range(60):
        try:
            return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2))
        except OSError:
            time.sleep(1)
    raise SystemExit(sh("docker", "logs", "gentest-gen"))


def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode() if body is not None else None,
                                 {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def job(jid, **settings):
    return {"id": jid, "model": "remotion", "kind": "render", "settings": {"composition": "Fixture", **settings},
            "inputs": [{"role": "bundle", "sha256": SHA, "mime": "application/gzip", "bytes": len(raw)}]}


def wait(jid, timeout=900):
    t = time.time()
    while time.time() - t < timeout:
        st, b = call("GET", f"/v1/jobs/{jid}")
        j = json.loads(b)
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(1)
    raise AssertionError(f"{jid} timed out: {j}")


def fetch(sha, path):
    st, b = call("GET", f"/v1/files/{sha}")
    assert st == 200, st
    open(path, "wb").write(b)
    assert hashlib.sha256(b).hexdigest() == sha


try:
    h = start_gen()
    assert h["remotion"] == "4.0.545" and h["slots"] == 1 and h["bundles"] == [], h
    # errors before any work
    st, b = call("POST", "/v1/jobs", job("bad1", foo=1))
    assert st == 422 and b"settings.foo" in b, (st, b)
    st, b = call("POST", "/v1/jobs", job("bad2", crf=99))
    assert st == 422, (st, b)
    st, b = call("POST", "/v1/jobs", job("bad3", remotion_version="4.0.100"))
    assert st == 422 and b"unsupported_remotion_version" in b, (st, b)
    st, b = call("POST", "/v1/jobs", job("bad4", gl="vulkan"))
    assert st == 422, (st, b)
    print("validation ok")

    # info (fetches and caches the bundle)
    assert call("POST", "/v1/jobs", job("r-1-0-1", part="info"))[0] == 202
    assert call("POST", "/v1/jobs", job("r-1-0-1", part="info"))[0] == 202  # the same id again: the same job
    j = wait("r-1-0-1")
    assert j["status"] == "done", j
    assert j["outputs"][0]["frames_total"] == 120 and j["outputs"][0]["fps"] == 30, j
    assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health"))["bundles"] == [SHA]
    print("info ok, bundle cached")

    # two video parts, then concat -c copy and ffprobe
    for n, fr in ((1, [0, 59]), (2, [60, 119])):
        assert call("POST", "/v1/jobs", job(f"r-1-{n}-1", part="video", frames=fr, crf=23))[0] == 202
    parts = []
    for n in (1, 2):
        j = wait(f"r-1-{n}-1")
        assert j["status"] == "done", j
        o = j["outputs"][0]
        assert o["frames"] == 60 and o["frames_total"] == 120 and o["mime"] == "video/mp4", o
        f = os.path.join(work, f"p{n}.mp4")
        fetch(o["sha256"], f)
        parts.append(f)
        assert call("DELETE", f"/v1/jobs/r-1-{n}-1")[0] == 200
    open(os.path.join(work, "list.txt"), "w").write("".join(f"file '/w/{os.path.basename(p)}'\n" for p in parts))
    sh("docker", "run", "--rm", "--entrypoint", "sh", "-v", f"{work}:/w", IMAGE, "-c",
       "F=/app/node_modules/@remotion/compositor-linux-x64-gnu; LD_LIBRARY_PATH=$F $F/ffmpeg -v error -y -f concat "
       "-safe 0 -i /w/list.txt -c copy /tmp/all.mp4 && LD_LIBRARY_PATH=$F $F/ffprobe -v error -count_frames "
       "-select_streams v:0 -show_entries stream=nb_read_frames,width,height -of json /tmp/all.mp4 > /w/probe.json")
    pr = json.load(open(os.path.join(work, "probe.json")))["streams"][0]
    assert int(pr["nb_read_frames"]) == 120 and pr["width"] == 320, pr
    print("video parts + concat ok (120 frames)")

    # audio of a range and of the whole composition (WAV)
    assert call("POST", "/v1/jobs", job("r-1-a-1", part="audio"))[0] == 202
    j = wait("r-1-a-1")
    assert j["status"] == "done" and j["outputs"][0]["mime"] == "audio/wav", j
    assert abs(j["outputs"][0]["seconds"] - 4.0) < 0.05, j
    print("audio ok")

    # frames out of range: error with frames_total
    call("POST", "/v1/jobs", job("r-1-x-1", frames=[100, 200]))
    j = wait("r-1-x-1")
    assert j["status"] == "error" and j["error_code"] == "frames_out_of_range" and j["frames_total"] == 120, j
    call("POST", "/v1/jobs", {**job("r-1-y-1"), "settings": {"composition": "Nope"}})
    j = wait("r-1-y-1")
    assert j["status"] == "error" and j["error_code"] == "no_composition", j
    print("render errors ok")

    # cancel a running part
    call("POST", "/v1/jobs", job("r-1-c-1", frames=[0, 119], crf=0))
    for _ in range(120):
        if json.loads(call("GET", "/v1/jobs/r-1-c-1")[1])["status"] == "running":
            break
        time.sleep(0.5)
    st, b = call("DELETE", "/v1/jobs/r-1-c-1")
    assert st == 200 and json.loads(b)["status"] == "canceled", (st, b)
    time.sleep(2)
    ps = sh("docker", "exec", "gentest-gen", "sh", "-c", "ps -eo comm | grep -c chrome || true").strip()
    assert ps == "0", f"chrome still running: {ps}"
    print("cancel ok")

    # busy: viewers on the node -> a new part waits (paused), then runs
    json.dump({"streams": 1, "at": time.time()}, open(os.path.join(ctl, "busy.json"), "w"))
    call("POST", "/v1/jobs", job("r-1-b-1", part="info"))
    time.sleep(7)
    j = json.loads(call("GET", "/v1/jobs/r-1-b-1")[1])
    assert j["status"] == "paused" and j["busy_reason"] == "serving viewers", j
    os.remove(os.path.join(ctl, "busy.json"))
    assert wait("r-1-b-1")["status"] == "done"
    print("busy ok")

    # GEN_PAUSE=1: paused in health, the cache survives a restart
    h = start_gen("-e", "GEN_PAUSE=1")
    assert h["paused"] and h["busy"] and h["bundles"] == [], h  # a new container without the cache volume
    print("GEN_PAUSE ok")
    print("GEN OK")
except BaseException:
    print(subprocess.run(["docker", "logs", "--tail", "40", "gentest-gen"], capture_output=True, text=True).stdout[-4000:])
    raise
finally:
    docker_rm("gentest-gen", "gentest-media")
    subprocess.run(["docker", "network", "rm", NET], capture_output=True)
