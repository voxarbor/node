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


def start_gen(*extra, limits=("--cpus", "3", "--memory", "4g")):
    docker_rm("gentest-gen")
    sh("docker", "run", "-d", "--name", "gentest-gen", "--network", NET, "-p", f"127.0.0.1:{PORT}:8000",
       *limits, "--cpu-shares", "128", "-e", f"API_KEY={KEY}", "-e", "GEN_MEDIA_URL=http://gentest-media:8000",
       "-e", "TTS_URL=http://nowhere:1", "-v", f"{ctl}:/ctl:ro", *extra, IMAGE)
    for _ in range(60):
        try:
            return json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2))
        except OSError:
            time.sleep(1)
    raise SystemExit(sh("docker", "logs", "gentest-gen"))


LAT = []  # seconds per GET /v1/jobs/{id} while parts render


def call(method, path, body=None):
    t = time.time()
    try:
        return _call(method, path, body)
    finally:
        if method == "GET" and path.startswith("/v1/jobs/"):
            LAT.append(time.time() - t)


def _call(method, path, body=None):
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

    # retry ids of the coordinator (2026-11-28): render r-<job>-<part>-<try>-a<k>, generation g-<id>-<k>; each is a
    # job of its own (not the earlier attempt's state); the longest allowed id is 100 characters
    long_id = "r-" + "9" * 90 + "-1-1-a12"
    long_id = long_id[:100]
    for jid in ("r-1-0-1-a1", "r-1-0-1-a2", "g-77-1", long_id):
        assert call("POST", "/v1/jobs", job(jid, part="info"))[0] == 202, jid
        j = wait(jid)
        assert j["id"] == jid and j["status"] == "done" and j["outputs"][0]["frames_total"] == 120, j
        st, b = call("DELETE", f"/v1/jobs/{jid}")
        assert st == 200, (jid, st, b)
    st, b = call("POST", "/v1/jobs", job("r-" + "9" * 100, part="info"))
    assert st == 422 and b"id:" in b, (st, b)
    print("retry ids (r-…-a<k>, g-…-<k>, 100 characters) ok")

    # two video parts, then concat -c copy and ffprobe
    for n, fr, conc in ((1, [0, 59], 0), (2, [60, 119], 64)):  # 64 > the container's 3 CPUs: clamped, not an error
        assert call("POST", "/v1/jobs", job(f"r-1-{n}-1", part="video", frames=fr, crf=23, concurrency=conc))[0] == 202
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
    print(f"video parts + concat ok (120 frames); GET /v1/jobs/<id> while rendering: max {max(LAT):.2f} s")
    assert max(LAT) < 5, f"status requests blocked during a render: max {max(LAT):.1f} s"

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

    # large bundles (~320 MB, three cold downloads from an HTTP/1.0 server that closes the connection): the
    # download must not take gen down (undici's fetch did, 3 of 6 times at 600 MB)
    import tarfile
    for k in range(3):
        big = os.path.join(work, "big.tgz")
        pack = os.path.join(work, "pack")
        os.makedirs(pack, exist_ok=True)
        with tarfile.open(os.path.join(media, SHA)) as t:
            t.extractall(pack, filter="data")
        open(os.path.join(pack, "padding.bin"), "wb").write(os.urandom(320 * 1024 * 1024))
        sh("tar", "-cf", big, "-C", pack, ".")
        braw = open(big, "rb").read()
        bsha = hashlib.sha256(braw).hexdigest()
        os.rename(big, os.path.join(media, bsha))
        sh("rm", "-rf", pack)
        body = {**job(f"r-big-{k}", part="info"), "inputs": [{"role": "bundle", "sha256": bsha, "bytes": len(braw)}]}
        assert call("POST", "/v1/jobs", body)[0] == 202
        j = wait(f"r-big-{k}")
        assert j["status"] == "done" and j["fetched_bytes"] == len(braw), j
        assert json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health"))["status"] == "ok"
        os.remove(os.path.join(media, bsha))
    print("large bundles ok (3 x 320 MB)")

    # GEN_PAUSE=1: paused in health, the cache survives a restart
    h = start_gen("-e", "GEN_PAUSE=1")
    assert h["paused"] and h["busy"] and h["bundles"] == [], h  # a new container without the cache volume
    print("GEN_PAUSE ok")

    # audit #22: two parts with the same bytes share one file; deleting the first keeps the second's file
    docker_rm("gentest-gen")
    subprocess.run(["docker", "volume", "rm", "gentest-cache"], capture_output=True)  # a clean cache for this part
    h = start_gen("-v", "gentest-cache:/cache")
    for jid in ("r-d-1", "r-d-2"):
        assert call("POST", "/v1/jobs", job(jid, part="audio"))[0] == 202
    o1, o2 = wait("r-d-1")["outputs"][0], wait("r-d-2")["outputs"][0]
    assert o1["sha256"] == o2["sha256"], (o1, o2)
    assert call("DELETE", "/v1/jobs/r-d-1")[0] == 200
    fetch(o2["sha256"], os.path.join(work, "shared.wav"))
    assert call("DELETE", "/v1/jobs/r-d-2")[0] == 200
    assert call("GET", f"/v1/files/{o2['sha256']}")[0] == 404  # gone with the last job
    print("#22 shared output survives the first delete ok")

    # audit #11: too little disk for the output -> disk_full and no partial file; leftovers of a previous run go
    sh("docker", "exec", "gentest-gen", "sh", "-c", f"mkdir -p /cache/bundles/.{'a' * 64}.1.part /cache/bundles/.{'b' * 64}.7.dir")
    h = start_gen("-v", "gentest-cache:/cache", "-e", "GEN_MIN_FREE_GB=100000")
    assert h["bundles"] == [SHA], h  # the cached bundle is kept
    left = sh("docker", "exec", "gentest-gen", "sh", "-c", "ls -A /cache/bundles /cache/out 2>/dev/null || true").split()
    assert not [x for x in left if x.endswith((".part", ".dir"))], left
    call("POST", "/v1/jobs", job("r-f-1", part="video", frames=[0, 29]))
    j = wait("r-f-1")
    assert j["status"] == "error" and j["error_code"] == "disk_full", j
    out = sh("docker", "exec", "gentest-gen", "sh", "-c", "ls -A /cache/out 2>/dev/null || true").split()
    assert not out, out
    print("#11 disk_full before rendering, no partial file, startup cleanup ok")
    docker_rm("gentest-gen")
    subprocess.run(["docker", "volume", "rm", "gentest-cache"], capture_output=True)

    # defaults without caps (0.18.6): cores from the machine, concurrency also bounded by RAM; and the compose file
    # gives gen cpu_shares 128 and no cpu / memory limit unless GEN_CPUS / GEN_MEM are set
    h = start_gen(limits=())
    nproc = int(sh("docker", "run", "--rm", "alpine:3.20", "nproc").strip())
    ram = int(sh("docker", "run", "--rm", "alpine:3.20", "sh", "-c", "awk '/MemTotal/ {print $2}' /proc/meminfo")) * 1024
    assert h["cores"] == nproc, (h["cores"], nproc)
    assert h["concurrency_default"] == max(1, min(nproc, int(ram * 0.75 // 1e9))), (h["concurrency_default"], nproc, ram)
    h = start_gen(limits=("--cpus", "3", "--memory", "4g"))
    assert h["cores"] == 3 and h["concurrency_default"] == 3 and abs(h["memory_gb"] - 4.3) < 0.1, h
    root = os.path.dirname(HERE)
    env = dict(os.environ, NODE_TOKEN="x", COMPOSE_PROFILES="gen")
    for k in ("GEN_CPUS", "GEN_MEM"):
        env.pop(k, None)
    for files in (["compose.yaml"], ["compose.yaml", "compose.gpu.yaml"]):
        cfg = json.loads(sh("docker", "compose", *[a for f in files for a in ("-f", os.path.join(root, f))],
                            "--project-directory", root, "config", "--format", "json", env=env))["services"]["gen"]
        lim = cfg["deploy"]["resources"]["limits"]
        assert cfg["cpu_shares"] == 128 and float(lim.get("cpus", 0)) == 0 and int(lim.get("memory", 0)) == 0, (files, cfg)
    print("defaults: all cores, RAM-bound concurrency, cpu_shares 128, no compose caps ok")
    print("GEN OK")
except BaseException:
    print(subprocess.run(["docker", "logs", "--tail", "40", "gentest-gen"], capture_output=True, text=True).stdout[-4000:])
    raise
finally:
    docker_rm("gentest-gen", "gentest-media")
    subprocess.run(["docker", "network", "rm", NET], capture_output=True)
