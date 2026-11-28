"""Release manifest for node-updater (docs/updater.md): node files with sha256 and every image of the CPU and GPU
configurations by digest. Run by .github/workflows/release.yml on a v* tag, after the images exist:
  python3 .github/release_manifest.py v0.13.0 > manifest.json"""
import hashlib, json, os, re, subprocess, sys

tag = sys.argv[1]
FILES = ("compose.yaml", "compose.gpu.yaml", "Caddyfile", "prune.sh")
env = dict(os.environ, NODE_TOKEN="x", VOXARBOR_NODE_TAG=tag, COMPOSE_PROFILES="fetch,init,gen")


def images(*files, extra=None):
    cmd = ["docker", "compose"] + [a for f in files for a in ("-f", f)] + ["config", "--images"]
    return subprocess.run(cmd, env=dict(env, **(extra or {})), capture_output=True, text=True, check=True).stdout.split()


def digest(ref):
    out = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
                         capture_output=True, text=True, check=True).stdout
    return ref.rsplit(":", 1)[0] + "@" + json.loads(out)["digest"]


refs = sorted(set(images("compose.yaml") + images("compose.yaml", "compose.gpu.yaml")))
opus = [r for r in refs if "/node-models-opus:" in r]
man = {
    "version": tag,
    "opus_models_tag": re.search(r"OPUS_MODELS_TAG:-(\w+)", open("compose.yaml").read()).group(1),
    "files": {f: hashlib.sha256(open(f, "rb").read()).hexdigest() for f in FILES},
    "images": {r: digest(r) for r in refs if r not in opus},
    "opus_image": {"ref": opus[0], "digest_ref": digest(opus[0])} if opus else None,
    "min_updater": "0.1.0",
}
bad = [r for r in man["images"] if f":{tag}" not in r and "/node-" in r]
if bad:
    sys.exit(f"images not at {tag}: {bad}")
# every node image of the release must be the build of this very commit (audit #19): images.yml tags what its run
# built or re-tagged for a commit as sha-<12>; v<tag> pointing elsewhere means a tag moved or a stale build
sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
moved = [r for r, d in man["images"].items() if "/node-" in r and digest(r.rsplit(":", 1)[0] + f":sha-{sha}") != d]
if moved:
    sys.exit(f"images at {tag} are not the build of commit {sha}: {moved}")
json.dump(man, sys.stdout, indent=1)
