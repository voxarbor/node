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
print("UPDATER RELEASE OK")
