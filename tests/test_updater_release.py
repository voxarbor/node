"""node-updater release handling, docker and GitHub mocked (no network, no containers):
  python3 tests/test_updater_release.py [path/to/updater.py]
Audit 2026-11-28: #4 (a manifest left by a failed signature check must never be used), #29 (.env rewrites never
readable by others)."""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile

UPD = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "services", "updater",
                                                          "updater.py")
spec = importlib.util.spec_from_file_location("updater", UPD)
upd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upd)
calls = []
upd.run = lambda cmd, timeout=3600, check=True, env=None: (calls.append(" ".join(cmd)),
                                                            subprocess.CompletedProcess(cmd, 0, "", ""))[1]
EVIL = "ghcr.io/voxarbor/node-models-opus@sha256:" + "e" * 64

# ---------- #4: a failed signature check leaves no manifest that a later step trusts ----------
wd = tempfile.mkdtemp()
cache = os.path.join(wd, ".updater")
man = {"version": "v9.9.9", "files": {}, "images": {}, "opus_models_tag": "9",
       "opus_image": {"ref": "ghcr.io/voxarbor/node-models-opus:9", "digest_ref": EVIL}}
upd.http_json = lambda url, headers=None, timeout=15: {"assets": [
    {"name": "manifest.json", "browser_download_url": "x://manifest.json"},
    {"name": "manifest.json.sigstore.json", "browser_download_url": "x://bundle"}]}


class Resp:
    def __init__(self, data): self.data = data
    def __enter__(self): import io; return io.BytesIO(self.data)
    def __exit__(self, *a): return False


upd.urllib.request.urlopen = lambda url, timeout=120: Resp(json.dumps(man).encode() if "manifest" in url else b"{}")


def bad_signature(*a):
    raise RuntimeError("signature: invalid")


upd.verify_signature = bad_signature
try:
    upd.fetch_release("v9.9.9", os.path.join(cache, "v9.9.9"))
    raise AssertionError("fetch_release accepted an unverified release")
except RuntimeError:
    pass
left = [os.path.join(r, f) for r, _, fs in os.walk(wd) for f in fs]
assert not left, f"unverified files left in the cache: {left}"

# an attacker's (or a half-checked) manifest on disk: repair must not pull from it
os.makedirs(os.path.join(cache, "v9.9.9"))
json.dump(man, open(os.path.join(cache, "v9.9.9", "manifest.json"), "w"))
p = object.__new__(upd.Project)
p.name, p.wd, p.env_file = "voxarbor-node", wd, os.path.join(wd, ".env")
u = object.__new__(upd.Updater)
u.p, u.cache, u.prepared, u._repaired, u._repair_at, u.idle_since = p, cache, {}, None, -1e9, 0.0
u.st = type("S", (), {"set": lambda self, **kw: None, "d": {}})()
u.opus_have = lambda: "1"
u.opus_needed = lambda m: True
u.repair("v9.9.9")
assert not [c for c in calls if EVIL in c], f"repair used an unverified manifest: {calls}"
print("#4 unverified manifest never used ok")

# ---------- #29: .env rewritten with a 022 umask is never readable by others ----------
os.umask(0o022)
env = os.path.join(wd, ".env")
open(env, "w").write("NODE_TOKEN=secret\nCOMPOSE_PROFILES=fetch\n")
os.chmod(env, 0o600)
seen = []
real_open = open


def watch_open(path, mode="r", *a, **kw):  # the mode of a file the updater creates, right when it is created
    f = real_open(path, mode, *a, **kw)
    if "w" in mode and (isinstance(path, int) or ".env" in os.path.basename(str(path))):
        seen.append(stat.S_IMODE(os.fstat(f.fileno()).st_mode))
    return f


upd.open = watch_open
p.env_write("COMPOSE_PROFILES", "fetch,gen")
del upd.open
assert seen and all(m & 0o077 == 0 for m in seen), [oct(m) for m in seen]
assert stat.S_IMODE(os.stat(env).st_mode) == 0o600 and "gen" in open(env).read()
assert not [f for f in os.listdir(wd) if f.startswith(".env") and f != ".env"], os.listdir(wd)
print("#29 .env temp file 0600 ok")

# ---------- #10: a service the node runs must be up after an update ----------
upd.TOKEN = "x"
caps = {"product_version": "v9.9.9", "services": {"tts": {"available": False, "healthy": False},
                                                  "translate": {"available": True, "healthy": True}}}
upd.http_json = lambda url, headers=None, timeout=15: caps if "capabilities" in url else {"enabled": True, "connected": True}
u.required_services = lambda: ["tts", "translate"]
ok, why = u.healthy("v9.9.9")
assert not ok and "tts" in (why or ""), (ok, why)
caps["services"]["tts"] = {"available": True, "healthy": True}
assert u.healthy("v9.9.9") == (True, None)
print("#10 a required service that is down fails the update ok")

# ---------- #43: the drain decides with fresh numbers in a later step ----------
wish = {"version": "v9.9.9"}
upd.desired = lambda: dict(wish)
upd.effective_mode = lambda d: "auto"
streams = [0]
tts_free = [True]
upd.busy_streams = lambda: streams[0]
upd.tts_idle = lambda: tts_free[0]
upd.WINDOW_S = 0
applied = []
u.p.tag = lambda: "v9.9.8"
u.st = type("S", (), {"set": lambda self, **kw: None, "d": {}})()
u.prepared, u.pulled, u.drain_since, u.idle_since = {"v9.9.9": {"files": {}}}, {"v9.9.9"}, None, None
u.apply = lambda tag, man: applied.append(tag)
u.drain_end = lambda: setattr(u, "drain_since", None)
u.repair = lambda cur: None
u.step()                       # quiet before: the drain starts, nothing is applied in the same step
assert not applied, "applied in the step that only started draining"
tts_free[0] = False            # a TTS job started meanwhile
u.step()
assert not applied, "applied while TTS was busy"
tts_free[0] = True
wish["paused"] = True          # and then the coordinator paused the node
u.step()
assert not applied and u.drain_since is None, "applied although paused"
wish.pop("paused")
u.step(); u.step()
assert applied == ["v9.9.9"], applied
print("#43 drain: fresh check, pause respected ok")

# ---------- #9: an update cut off mid-way (SIGTERM, power) is rolled back at the next start ----------
wd2 = tempfile.mkdtemp()
env2 = os.path.join(wd2, ".env")
open(env2, "w").write("VOXARBOR_NODE_TAG=v1.0.0\nOPUS_MODELS_TAG=2\n")
for n in ("compose.yaml", "Caddyfile"):
    open(os.path.join(wd2, n), "w").write("old " + n)
cache2 = os.path.join(wd2, ".updater")
os.makedirs(os.path.join(cache2, "v2.0.0"))
for n in ("compose.yaml", "Caddyfile"):
    open(os.path.join(cache2, "v2.0.0", n), "w").write("new " + n)
p2 = object.__new__(upd.Project)
p2.name, p2.wd, p2.env_file = "voxarbor-node", wd2, env2
p2.own = lambda path: None
u2 = object.__new__(upd.Updater)
u2.p, u2.cache = p2, cache2
u2.st = type("S", (), {"set": lambda self, **kw: None, "d": {}})()
u2.opus_needed = lambda m: False
u2.gateway_sync = lambda only_when_quiet=False: False


def killed_up(files=None):
    raise SystemExit("SIGTERM")  # not an Exception: the in-process rollback does not run


u2.up = killed_up
try:
    u2.apply("v2.0.0", {"files": {"compose.yaml": "", "Caddyfile": ""}, "opus_models_tag": "3"})
except SystemExit:
    pass
assert "VOXARBOR_NODE_TAG=v2.0.0" in open(env2).read()   # the disk says new, the containers are old
u3 = object.__new__(upd.Updater)                          # the updater starts again
u3.p, u3.cache, u3.st = p2, cache2, u2.st
u3.up, u3.gateway_sync = (lambda files=None: None), (lambda only_when_quiet=False: False)
assert u3.recover_interrupted() is True
e = open(env2).read()
assert "VOXARBOR_NODE_TAG=v1.0.0" in e and "OPUS_MODELS_TAG=2" in e, e
assert open(os.path.join(wd2, "compose.yaml")).read() == "old compose.yaml"
assert u3.recover_interrupted() is False                  # done once
print("#9 interrupted update rolled back at the next start (tag, files, opus pin) ok")
print("UPDATER RELEASE OK")
