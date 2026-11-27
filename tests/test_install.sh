#!/bin/sh
# install.sh against fake docker and curl (no network, nothing installed):
#   sh tests/test_install.sh            (from the repository root; CI: .github/workflows/checks.yml)
# Checks: a failed image pull stops the installation; .env is 0600 and never readable by others while written;
# --uninstall honours --dry-run and refuses folders that are not a node; plain http only for this machine.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/rel" "$T/home"
fail() { echo "FAIL: $*" >&2; exit 1; }

# ---------- a fake release (the signature check is the fake docker's cosign run: it passes) ----------
for f in compose.yaml compose.gpu.yaml Caddyfile prune.sh; do cp "$ROOT/$f" "$T/rel/$f"; done
cp "$ROOT/.env.example" "$T/rel/env.example"
DIG="ghcr.io/voxarbor/node-tts@sha256:$(printf '%064d' 1)"
cat > "$T/rel/manifest.json" <<EOF
{
 "version": "v0.0.1",
 "images": {
  "ghcr.io/voxarbor/node-tts:v0.0.1": "$DIG"
 }
}
EOF
(cd "$T/rel" && sha256sum compose.yaml compose.gpu.yaml Caddyfile prune.sh env.example > SHA256SUMS)
: > "$T/rel/SHA256SUMS.sigstore.json"; : > "$T/rel/manifest.json.sigstore.json"

# curl: -o FILE URL -> copies the release file by its name
cat > "$T/bin/curl" <<EOF
#!/bin/sh
out=""; url=""
while [ \$# -gt 0 ]; do case "\$1" in -o) out="\$2"; shift ;; -*) ;; *) url="\$1" ;; esac; shift; done
cp "$T/rel/\$(basename "\$url")" "\$out"
EOF
# docker: info / compose version / cosign run succeed; pull fails when FAIL_PULL=1; .env modes are recorded
cat > "$T/bin/docker" <<EOF
#!/bin/sh
echo "\$*" >> "$T/docker.log"
[ -f .env ] && stat -c '%a' .env >> "$T/envmodes"
case "\$1 \${2:-}" in
  "info "*) exit 0 ;;
  "compose version") exit 0 ;;
  "compose config") echo "ghcr.io/voxarbor/node-tts:v0.0.1"; exit 0 ;;
  "run "*) exit 0 ;;
  "pull "*) [ "\${FAIL_PULL:-}" = 1 ] && exit 1; exit 0 ;;
  "tag "*) exit 0 ;;
  "compose "*) exit 0 ;;
esac
exit 0
EOF
# sleep: no waiting in the pull retries
printf '#!/bin/sh\nexit 0\n' > "$T/bin/sleep"
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" HOME="$T/home"

# ---------- 1. a failed pull stops the installation, .env stays 0600 ----------
umask 022
out=$(cd "$T" && FAIL_PULL=1 NODE_DIR="$T/node" sh "$ROOT/install.sh" --coordinator https://c.invalid --join j_1 \
      --services stt --gpu off 2>&1) && fail "install went on after a failed pull: $out"
echo "$out" | grep -q "не вдалося завантажити образ ghcr.io/voxarbor/node-tts:v0.0.1" || fail "no pull error: $out"
grep -q "compose up" "$T/docker.log" && fail "compose up ran after a failed pull"
[ "$(stat -c '%a' "$T/node/.env")" = 600 ] || fail ".env is $(stat -c '%a' "$T/node/.env")"
grep -qv '^600$' "$T/envmodes" && fail ".env was readable by others at some point: $(sort -u "$T/envmodes" | tr '\n' ' ')"
[ ! -e "$T/node/.env.new" ] || fail ".env.new left behind"
ls -l "$T/node/.env" | grep -q '^-rw-------' || fail ".env mode"
echo "pull failure stops the install; .env 0600 all the time ok"

# ---------- 2. uninstall: --dry-run touches nothing; odd folders are refused ----------
: > "$T/docker.log"
out=$(NODE_DIR="$T/node" sh "$ROOT/install.sh" --uninstall --dry-run 2>&1) || fail "dry-run uninstall failed: $out"
[ -d "$T/node" ] || fail "dry-run removed the node folder"
grep -q "down" "$T/docker.log" && fail "dry-run ran docker compose down"
for d in / "$T/home" "$T"; do   # the root, $HOME, a folder without a node
  NODE_DIR="$d" sh "$ROOT/install.sh" --uninstall --yes >/dev/null 2>&1 && fail "uninstall accepted $d"
done
ln -s "$T/home" "$T/homelink"
NODE_DIR="$T/homelink/" sh "$ROOT/install.sh" --uninstall --yes >/dev/null 2>&1 && fail "uninstall accepted a link to \$HOME"
[ -d "$T/home" ] || fail "\$HOME removed"
NODE_DIR="$T/node" sh "$ROOT/install.sh" --uninstall --yes >/dev/null 2>&1 || fail "real uninstall failed"
[ ! -e "$T/node" ] || fail "node folder still there"
grep -q "compose --env-file .env down -v --rmi all" "$T/docker.log" || fail "no compose down"
echo "uninstall: dry-run, refused folders, real removal ok"

# ---------- 3. coordinator address: https, plain http only for this machine ----------
sh "$ROOT/install.sh" --dry-run --coordinator http://c.example --join x >/dev/null 2>&1 && fail "http accepted"
for c in https://c.example http://localhost:8000 http://127.0.0.1 "http://[::1]:9"; do
  sh "$ROOT/install.sh" --dry-run --coordinator "$c" --join x >/dev/null 2>&1 || fail "$c refused"
done
echo "coordinator: https only, http for localhost ok"
echo "INSTALL OK"
