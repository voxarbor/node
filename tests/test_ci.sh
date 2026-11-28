#!/bin/sh
# CI release safety (audit #19), no network:  sh tests/test_ci.sh
#  - .github/changed.sh sees a service change in an earlier commit of a multi-commit push (HEAD~1 alone missed it)
#  - .github/release_manifest.py refuses a release whose v<tag> image is not the build of the tagged commit
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

# ---------- changed.sh over the whole push ----------
cd "$T" && git init -q r && cd r && git config user.email t@t && git config user.name t
mkdir -p services/tts && echo a > services/tts/f && echo x > README && git add -A && git commit -qm base
before=$(git rev-parse HEAD)
echo b > services/tts/f && git commit -qam "tts change"
echo y > README && git commit -qam "docs only"          # the push = 2 commits, the last one does not touch tts
sh "$ROOT/.github/changed.sh" "$before" HEAD '^services/tts/' || fail "tts change in an earlier commit missed"
sh "$ROOT/.github/changed.sh" "$before" HEAD '^services/stt/' && fail "stt reported changed"
sh "$ROOT/.github/changed.sh" 0000000000000000000000000000000000000000 HEAD '^services/stt/' || fail "zero before must build"
sh "$ROOT/.github/changed.sh" "" HEAD '^x$' || fail "empty before must build"
sh "$ROOT/.github/changed.sh" deadbeefdeadbeefdeadbeefdeadbeefdeadbeef HEAD '^x$' || fail "unknown before must build"
echo "changed.sh: multi-commit push, unknown before ok"

# ---------- release_manifest.py: v<tag> must be the sha-<commit> build ----------
cp "$ROOT/compose.yaml" "$ROOT/compose.gpu.yaml" "$ROOT/Caddyfile" "$ROOT/prune.sh" "$ROOT/.github/release_manifest.py" .
git add -A && git commit -qm files
mkdir -p "$T/bin"
cat > "$T/bin/docker" <<'EOF'
#!/bin/sh
# compose config --images: two node images at the tag; imagetools inspect: digest by tag, MOVED makes v-tags differ
if [ "$1" = compose ]; then printf 'ghcr.io/voxarbor/node-tts:%s\nghcr.io/voxarbor/node-models-opus:3\ncaddy:2.11.7\n' "$VOXARBOR_NODE_TAG"; exit 0; fi
ref=$4; d=1; case "$ref" in *:v*) [ "${MOVED:-}" = 1 ] && d=2 ;; esac
printf '{"digest":"sha256:%064d"}\n' "$d"
EOF
chmod +x "$T/bin/docker"
PATH="$T/bin:$PATH" python3 release_manifest.py v9.9.9 >/dev/null || fail "a consistent release refused"
PATH="$T/bin:$PATH" MOVED=1 python3 release_manifest.py v9.9.9 >/dev/null 2>&1 && fail "a moved v-tag was signed"
echo "release_manifest: refuses a v-tag that is not the commit's build ok"
echo "CI OK"
