"""node-updater (docs/updater.md): brings this node to the version the coordinator asks for, only from a signed
GitHub release of voxarbor/node, only in an idle window, and rolls back if the node does not come up healthy.

Inputs (volume updater-ctl, written by the connector): desired.json {version, mode, now, paused, received_at} and
busy.json {streams, at}. Output: status.json (node-info shows it in /capabilities.updater, the connector sends its
state and seq in every ping). The coordinator can only name a version; files, digests and the signature come from
GitHub and are verified here.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

VERSION = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")).read().strip()
REPO = os.environ.get("UPDATER_REPO", "voxarbor/node")
CTL = os.environ.get("UPDATER_CTL", "/ctl")
ENV_MODE = os.environ.get("UPDATER_MODE", "auto").strip().lower()
HEALTH_MIN = float(os.environ.get("UPDATER_HEALTH_MIN", "10"))
WINDOW_S = float(os.environ.get("UPDATER_WINDOW_S", "120"))
ALLOW_DOWNGRADE = os.environ.get("UPDATER_ALLOW_DOWNGRADE", "") == "1"
GENTLE = os.environ.get("NODE_MODE", "") == "gentle"
TOKEN = os.environ.get("API_KEY", "")
GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8080")
TTS = os.environ.get("TTS_HEALTH_URL", "http://tts:8000/health")
CONNECTOR = os.environ.get("CONNECTOR_HEALTH_URL", "http://connector:8000/health")
RELEASE_FILES = ("compose.yaml", "compose.gpu.yaml", "Caddyfile", "prune.sh")
WORKFLOW = ".github/workflows/release.yml"
OIDC = "https://token.actions.githubusercontent.com"
MODES = ("auto", "notify", "off")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
DIGEST_RE = re.compile(r"^(?P<repo>[a-z0-9./_-]+)@sha256:[0-9a-f]{64}$")
# images a release may bring: our own and the two pinned upstream ones (STT, gateway); always by digest
ALLOWED_REPOS = re.compile(r"^(ghcr\.io/voxarbor/node-[a-z0-9-]+|ghcr\.io/speaches-ai/speaches|caddy)$")


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), *a, flush=True)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ver(tag):
    m = TAG_RE.match(tag or "")
    return tuple(map(int, m.groups())) if m else None


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def http_json(url, headers=None, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
        return json.loads(r.read())


def run(cmd, timeout=3600, check=True, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])}…: {(r.stderr or r.stdout).strip()[-400:]}")
    return r


# ---------------------------------------------------------------- how the owner runs compose ---------------------
class Project:
    """Reads this container's compose labels: project name, working dir, compose files, env file. The node folder
    is mounted into the updater; host paths are made visible at the same absolute path (symlinks), so compose
    resolves ./Caddyfile etc. exactly as on the host."""

    def __init__(self):
        me = json.loads(run(["docker", "inspect", socket.gethostname()]).stdout)[0]
        self.container = me["Id"]
        self.image = me["Config"]["Image"]
        lb = me["Config"]["Labels"]
        self.name = lb["com.docker.compose.project"]
        self.wd = lb["com.docker.compose.project.working_dir"]
        self.files = lb["com.docker.compose.project.config_files"].split(",")
        self.env_file = (lb.get("com.docker.compose.project.environment_file") or "").split(",")[0] \
            or os.path.join(self.wd, ".env")
        for m in me.get("Mounts", []):
            src, dst = m.get("Source"), m.get("Destination")
            if m.get("Type") == "bind" and src and dst and src != dst and not os.path.exists(src):
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.symlink(dst, src)
        for p in [self.wd, self.env_file, *self.files]:
            if not os.path.exists(p):
                raise RuntimeError(f"{p} is not visible to the updater: mount it (docs/updater.md) or set UPDATER_MODE=off")

    def compose(self, files=None, extra_env=None):
        cmd = ["docker", "compose", "-p", self.name, "--project-directory", self.wd, "--env-file", self.env_file]
        for f in files or self.files:
            cmd += ["-f", f]
        env = dict(os.environ, **(extra_env or {}))
        for k in ("NODE_MODE", "API_KEY", "UPDATER_MODE"):  # never let our own env override the owner's .env
            env.pop(k, None)
        return cmd, env

    def env_get(self, key):
        for line in open(self.env_file):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
        return None

    def tag(self):
        return self.env_get("VOICE_CORE_TAG")

    def set_tag(self, tag):
        self.env_write("VOICE_CORE_TAG", tag)

    def env_write(self, key, value):
        """set (value) or remove (None) one KEY= line of the owner's .env; nothing else changes"""
        lines = open(self.env_file).read().splitlines()
        out, done = [], False
        for line in lines:
            if line.startswith(key + "="):
                if value is None:
                    continue
                line, done = f"{key}={value}", True
            out.append(line)
        if value is not None and not done:
            out.append(f"{key}={value}")
        st = os.stat(self.env_file)
        tmp = self.env_file + ".updater-tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, st.st_mode & 0o7777)
        os.chown(tmp, st.st_uid, st.st_gid)  # the owner must keep running docker compose himself
        os.replace(tmp, self.env_file)

    def own(self, path):
        """files and folders the updater creates in the node folder belong to the folder's owner, not root"""
        st = os.stat(self.wd)
        paths = [path]
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                paths += [os.path.join(root, x) for x in dirs + files]
        for n in paths:
            try:
                os.lchown(n, st.st_uid, st.st_gid)
            except OSError:
                pass


# ---------------------------------------------------------------- status -----------------------------------------
class Status:
    def __init__(self):
        self.path = os.path.join(CTL, "status.json")
        old = read_json(self.path, {}) or {}
        self.d = {"version": VERSION, "mode": ENV_MODE, "state": "idle", "desired": None, "available": None,
                  "error": None, "seq": int(old.get("seq", 0)), "last_update": old.get("last_update"),
                  "failed": old.get("failed", {}), "pending_self": old.get("pending_self"), "warnings": []}
        self.save()

    def set(self, **kw):
        public = ("mode", "state", "desired", "available", "error", "last_update", "warnings")
        if any(self.d.get(k) != v for k, v in kw.items() if k in public):
            self.d["seq"] += 1
        self.d.update(kw)
        self.save()

    def save(self):
        write_json(self.path, {**self.d, "at": now_iso()})


# ---------------------------------------------------------------- release ----------------------------------------
def fetch_release(tag, dest):
    """Downloads the release's node files + manifest, verifies the signature (cosign keyless, our workflow on that
    tag) and every file's sha256. Returns the manifest."""
    rel = http_json(f"https://api.github.com/repos/{REPO}/releases/tags/{tag}", {"Accept": "application/vnd.github+json"})
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    for n in ("manifest.json", "manifest.json.sigstore.json"):
        if n not in assets:
            raise RuntimeError(f"release {tag} has no {n}")
    os.makedirs(dest, exist_ok=True)

    def get(name):
        with urllib.request.urlopen(assets[name], timeout=120) as r, open(os.path.join(dest, name), "wb") as f:
            shutil.copyfileobj(r, f)

    get("manifest.json")
    get("manifest.json.sigstore.json")
    identity = f"https://github.com/{REPO}/{WORKFLOW}@refs/tags/{tag}"
    run(["cosign", "verify-blob", os.path.join(dest, "manifest.json"), "--bundle",
         os.path.join(dest, "manifest.json.sigstore.json"), "--certificate-identity", identity,
         "--certificate-oidc-issuer", OIDC], timeout=300)
    man = read_json(os.path.join(dest, "manifest.json"))
    if man.get("version") != tag:
        raise RuntimeError(f"manifest is for {man.get('version')}, not {tag}")
    for ref, dref in list(man.get("images", {}).items()) + (
            [(man["opus_image"]["ref"], man["opus_image"]["digest_ref"])] if man.get("opus_image") else []):
        m = DIGEST_RE.match(dref or "")
        if not m or not ALLOWED_REPOS.match(m["repo"]) or ref.rsplit(":", 1)[0] != m["repo"]:
            raise RuntimeError(f"manifest image not allowed: {ref} -> {dref}")
    for name, sha in man["files"].items():
        if name not in RELEASE_FILES or name not in assets:
            raise RuntimeError(f"unexpected or missing file {name}")
        get(name)
        if hashlib.sha256(open(os.path.join(dest, name), "rb").read()).hexdigest() != sha:
            raise RuntimeError(f"{name}: sha256 mismatch")
    return man


# ---------------------------------------------------------------- node load --------------------------------------
def busy_streams():
    """active coordinator streams, or None when unknown (connector silent for > 30 s)"""
    b = read_json(os.path.join(CTL, "busy.json"))
    if not b or time.time() - float(b.get("at", 0)) > 30:
        return None
    return int(b.get("streams", 0))


def tts_idle():
    try:
        h = http_json(TTS, timeout=10)
        return not h.get("busy") and not sum((h.get("jobs") or {}).values())
    except Exception:  # noqa: BLE001
        return True  # tts not running is not a reason to wait


def desired():
    d = read_json(os.path.join(CTL, "desired.json"), {}) or {}
    return d


def effective_mode(d):
    cm = (d.get("mode") or "auto").lower()
    em = ENV_MODE if ENV_MODE in MODES else "auto"
    cm = cm if cm in MODES else "auto"
    return max(em, cm, key=MODES.index)  # the stricter wins: the owner of the machine can always say off


# ---------------------------------------------------------------- the update ------------------------------------
class Updater:
    def __init__(self):
        self.st = Status()
        self.p = Project()
        self.cache = os.path.join(self.p.wd, ".updater")
        self.prepared = {}  # tag -> manifest (verified, files in cache/<tag>)
        self.pulled = set()  # tags whose images are local
        self.idle_since = None
        self._repaired = None  # release whose opus models were checked (repair)
        self._repair_at = 0.0

    def files_for(self, tag):
        """the owner's compose files with the release's own ones taken from the verified cache"""
        out = []
        for f in self.p.files:
            b = os.path.basename(f)
            out.append(os.path.join(self.cache, tag, b) if os.path.dirname(f) == self.p.wd and b in RELEASE_FILES else f)
        return out

    def images(self, tag, man):
        cmd, env = self.p.compose(self.files_for(tag), {"VOICE_CORE_TAG": tag})
        refs = run(cmd + ["config", "--images"], env=env).stdout.split()
        need = {}
        for ref in sorted(set(refs)):
            if ref in man["images"]:
                need[ref] = man["images"][ref]
            elif not run(["docker", "image", "inspect", ref], check=False).returncode == 0:
                raise RuntimeError(f"{ref} is neither in the release manifest nor present locally")
        return need

    def opus_pin_fix(self, man):
        """an old installer wrote OPUS_MODELS_TAG=1 into .env, which overrides compose's newer default: a pin lower
        than the release's models version is removed (a higher one, set on purpose, stays)"""
        pin, want = self.p.env_get("OPUS_MODELS_TAG"), str(man.get("opus_models_tag", "1"))
        if pin and pin.isdigit() and want.isdigit() and int(pin) < int(want):
            self.p.env_write("OPUS_MODELS_TAG", None)
            log(f"removed the stale OPUS_MODELS_TAG={pin} from .env (release models: {want})")

    def opus_have(self):
        cmd, env = self.p.compose()
        r = run(cmd + ["run", "--rm", "--no-deps", "--entrypoint", "cat", "translate",
                       "/cache/opus-mt/.models-version"], check=False, env=env, timeout=300)
        return r.stdout.strip() or None

    def opus_install(self, man):
        """copies the release's Opus-MT models into the translate volume (init-opus), with the manifest's version"""
        want = str(man.get("opus_models_tag", "1"))
        cmd, env = self.p.compose(extra_env={"OPUS_MODELS_TAG": want})
        run(cmd + ["--profile", "init", "run", "--rm", "init-opus"], env=env, timeout=1800)

    def opus_needed(self, man):
        want = str(man.get("opus_models_tag", "1"))
        cmd, env = self.p.compose()
        services = run(cmd + ["--profile", "init", "config", "--services"], env=env, check=False).stdout.split()
        if "translate" not in services or "init-opus" not in services:
            return False  # a node (or a test stand) without the Opus-MT models volume
        r = run(cmd + ["run", "--rm", "--no-deps", "--entrypoint", "cat", "translate",
                       "/cache/opus-mt/.models-version"], check=False, env=env, timeout=300)
        return r.stdout.strip() != want

    def pull(self, tag, man, may_run):
        """pulls by digest and tags; stops (returns False) as soon as may_run() says no"""
        need = self.images(tag, man)
        if self.opus_needed(man) and man.get("opus_image"):
            need[man["opus_image"]["ref"]] = man["opus_image"]["digest_ref"]
        for ref, dref in need.items():
            if run(["docker", "image", "inspect", dref], check=False).returncode == 0:
                run(["docker", "tag", dref, ref])
                continue
            proc = subprocess.Popen(["docker", "pull", "-q", dref], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True)
            while proc.poll() is None:
                time.sleep(2)
                if not may_run():
                    proc.send_signal(signal.SIGINT)
                    proc.wait(30)
                    return False
            if proc.returncode:
                raise RuntimeError(f"pull {dref}: {proc.stderr.read()[-300:]}")
            run(["docker", "tag", dref, ref])
        return True

    def healthy(self, tag):
        try:
            caps = http_json(f"{GATEWAY}/capabilities", {"Authorization": f"Bearer {TOKEN}"}, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return False, f"capabilities: {type(exc).__name__}"
        if caps.get("product_version") != tag:
            return False, f"node reports {caps.get('product_version')}"
        bad = [k for k, s in (caps.get("services") or {}).items() if s.get("available") and not s.get("healthy")]
        if bad:
            return False, "unhealthy: " + ",".join(bad)
        try:
            c = http_json(CONNECTOR, timeout=10)
            if c.get("enabled") and not c.get("connected"):
                return False, "connector not connected"
        except Exception as exc:  # noqa: BLE001
            return False, f"connector: {type(exc).__name__}"
        return True, None

    def up(self, files=None):
        cmd, env = self.p.compose(files)
        services = [s for s in run(cmd + ["config", "--services"], env=env).stdout.split() if s != "updater"]
        run(cmd + ["up", "-d", "--pull", "never"] + services, env=env, timeout=1800)

    def apply(self, tag, man):
        cur = self.p.tag()
        prev = os.path.join(self.cache, "prev")
        shutil.rmtree(prev, ignore_errors=True)
        os.makedirs(prev)
        for n in RELEASE_FILES:
            if os.path.exists(os.path.join(self.p.wd, n)):
                shutil.copy2(os.path.join(self.p.wd, n), prev)
        write_json(os.path.join(prev, "tag.json"), {"tag": cur})
        self.st.set(state="updating", error=None)
        log(f"updating {cur} -> {tag}")
        try:
            for n in man["files"]:
                dst = os.path.join(self.p.wd, n)
                shutil.copyfile(os.path.join(self.cache, tag, n), dst)  # an existing file keeps owner and mode
                if n.endswith(".sh"):
                    os.chmod(dst, 0o755)
                self.p.own(dst)
            self.p.own(self.cache)
            self.p.set_tag(tag)
            self.opus_pin_fix(man)
            if self.opus_needed(man):
                self.opus_install(man)
            self.up()
            self.st.set(state="checking")
            deadline, ok_since, why = time.time() + HEALTH_MIN * 60, None, "timeout"
            while time.time() < deadline:
                ok, why = self.healthy(tag)
                ok_since = (ok_since or time.time()) if ok else None
                if ok_since and time.time() - ok_since >= 30:
                    break
                time.sleep(10)
            else:
                raise RuntimeError(f"not healthy after {HEALTH_MIN:.0f} min: {why}")
        except Exception as exc:  # noqa: BLE001
            log("update failed:", exc, "- rolling back to", cur)
            for n in RELEASE_FILES:
                if os.path.exists(os.path.join(prev, n)):
                    shutil.copy2(os.path.join(prev, n), os.path.join(self.p.wd, n))
            if cur:
                self.p.set_tag(cur)
            try:
                self.up()
            except Exception as exc2:  # noqa: BLE001
                log("rollback up failed:", exc2)
            failed = dict(self.st.d.get("failed") or {}, **{tag: now_iso()})
            self.st.set(state="rolled_back", error=str(exc)[:300], failed=failed,
                        last_update={"from": cur, "to": tag, "ok": False, "at": now_iso(), "error": str(exc)[:300]})
            return
        self.st.set(state="idle", error=None,
                    last_update={"from": cur, "to": tag, "ok": True, "at": now_iso(), "error": None})
        log(f"updated to {tag}")
        cmd, env = self.p.compose()
        pr = run(["sh", os.path.join(self.p.wd, "prune.sh")], check=False,
                 env=dict(env, COMPOSE=" ".join(cmd)), timeout=1800)
        log("prune:", (pr.stdout or pr.stderr).strip().splitlines()[-1:] or "")
        self.replace_self()

    def replace_self(self):
        """a container cannot recreate itself through compose: a one-shot copy of the new updater image does it,
        with this container's mounts (--volumes-from) and the same compose arguments"""
        cmd, env = self.p.compose()
        new = next((ref for ref in run(cmd + ["config", "--images"], env=env).stdout.split() if "/node-updater:" in ref), None)
        if not new or new == self.p.image:
            return
        self.st.set(pending_self=now_iso())
        log("replacing myself with", new)
        run(["docker", "run", "-d", "--rm", "--volumes-from", self.p.container, "--network", "none",
             "-e", "UPDATER_REPLACE=" + json.dumps(cmd), "--entrypoint", "python", new, "updater.py", "replace"])

    # ------------------------------------------------------------ main loop
    def repair(self, cur):
        """the node runs release `cur`, but its Opus-MT models are older than that release's: report it, and in an
        idle window (not paused, mode not off) pull the models image by digest and copy the models"""
        if not ver(cur) or self._repaired == cur or time.monotonic() - self._repair_at < 60:
            return
        self._repair_at = time.monotonic()  # each check starts a short container: at most once a minute
        try:
            man = self.prepared.get(cur) or read_json(os.path.join(self.cache, cur, "manifest.json"))
            if not man:
                man = self.prepared[cur] = fetch_release(cur, os.path.join(self.cache, cur))
            if not self.opus_needed(man):
                self._repaired = cur
                self.st.set(warnings=[])
                return
            have, want = self.opus_have(), str(man.get("opus_models_tag"))
            self.st.set(warnings=[f"opus models {have} on the node, release {cur} has {want}"])
            if effective_mode(desired()) == "off" or desired().get("paused"):
                return
            quiet = busy_streams() in (0, None) and tts_idle()
            self.idle_since = (self.idle_since or time.time()) if quiet else None
            if self.idle_since is None or time.time() - self.idle_since < WINDOW_S:
                return
            log(f"repair: opus models {have} -> {want}")
            self.opus_pin_fix(man)
            oi = man.get("opus_image")
            if oi:
                run(["docker", "pull", "-q", oi["digest_ref"]], timeout=3600)
                run(["docker", "tag", oi["digest_ref"], oi["ref"]])
            self.opus_install(man)
            cmd, env = self.p.compose()
            run(cmd + ["restart", "translate"], env=env, timeout=300)
            pr = run(["sh", os.path.join(self.p.wd, "prune.sh")], check=False, env=dict(env, COMPOSE=" ".join(cmd)),
                     timeout=1800)
            log("repair done; prune:", (pr.stdout or pr.stderr).strip().splitlines()[-1:] or "")
            self._repaired = cur
            self.st.set(warnings=[])
        except Exception as exc:  # noqa: BLE001
            log("repair:", exc)

    def step(self):
        d = desired()
        mode = effective_mode(d)
        tag = d.get("version")
        cur = self.p.tag()
        self.st.set(mode=mode, desired=tag)
        if d.get("paused"):
            self.st.set(state="paused")
            self.idle_since = None
            return
        if not ver(tag) or not ver(cur) or tag == cur:
            self.st.set(state="idle", available=None)
            self.repair(cur)
            return
        if ver(tag) < ver(cur) and not ALLOW_DOWNGRADE:  # e.g. a coordinator that has not caught up yet
            self.st.set(state="idle", available=None, error=f"desired {tag} is older than {cur}: ignored")
            return
        failed_at = (self.st.d.get("failed") or {}).get(tag)
        if failed_at and not (d.get("now") and d.get("received_at", "") > failed_at):
            self.st.set(state="rolled_back")
            return
        self.st.set(available=tag)
        if mode == "off" or (mode == "notify" and not d.get("now")):
            self.st.set(state="available")
            return
        streams = busy_streams()
        quiet = streams == 0 and tts_idle()
        self.idle_since = (self.idle_since or time.time()) if quiet else None
        in_window = self.idle_since is not None and time.time() - self.idle_since >= WINDOW_S
        if tag not in self.prepared:
            self.st.set(state="downloading")
            self.prepared[tag] = fetch_release(tag, os.path.join(self.cache, tag))
        man = self.prepared[tag]
        if tag not in self.pulled:
            # downloads never compete with live work: only while no stream runs (gentle: only in the idle window)
            ok_now = in_window if GENTLE else streams == 0
            if not ok_now:
                self.st.set(state="waiting_window")
                return
            self.st.set(state="downloading")
            if not self.pull(tag, man, lambda: busy_streams() == 0 and not desired().get("paused")):
                self.st.set(state="waiting_window")
                return
            self.pulled.add(tag)
        if not in_window:
            self.st.set(state="waiting_window")
            return
        self.apply(tag, man)

    def loop(self):
        log(f"node-updater {VERSION}, project {self.p.name} in {self.p.wd}, mode {ENV_MODE}")
        if self.st.d.get("pending_self"):
            # first start after replacing myself: the previous updater image is free now, prune it too
            cmd, env = self.p.compose()
            pr = run(["sh", os.path.join(self.p.wd, "prune.sh")], check=False, env=dict(env, COMPOSE=" ".join(cmd)),
                     timeout=1800)
            log("prune after self-replacement:", (pr.stdout or pr.stderr).strip().splitlines()[-1:] or "")
            self.st.set(pending_self=None)
        while True:
            try:
                self.step()
            except Exception as exc:  # noqa: BLE001
                log("error:", exc)
                self.st.set(state="error", error=str(exc)[:300])
                time.sleep(60)
            time.sleep(15)


def replace():
    """helper mode (one-shot container of the new image): recreate the updater service, then exit"""
    cmd = json.loads(os.environ["UPDATER_REPLACE"])
    me = json.loads(run(["docker", "inspect", socket.gethostname()]).stdout)[0]
    for m in me.get("Mounts", []):
        src, dst = m.get("Source"), m.get("Destination")
        if m.get("Type") == "bind" and src and dst and src != dst and not os.path.exists(src):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            os.symlink(dst, src)
    time.sleep(3)
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_MODE", "API_KEY", "UPDATER_MODE")}
    r = run(cmd + ["up", "-d", "--no-deps", "--pull", "never", "updater"], check=False, env=env)
    log("replace:", r.returncode, (r.stderr or r.stdout).strip()[-300:])


if __name__ == "__main__":
    if sys.argv[1:] == ["replace"]:
        replace()
        sys.exit(0)
    os.makedirs(CTL, exist_ok=True)
    os.chown(CTL, 65534, 65534)  # the connector (nobody) writes desired.json / busy.json here
    if ENV_MODE == "off":
        st = Status()
        st.set(mode="off", state="off")
        log("UPDATER_MODE=off: reporting only")
        while True:
            d = desired()
            st.set(desired=d.get("version"))
            time.sleep(30)
    Updater().loop()
