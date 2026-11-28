#!/bin/sh
# fetch /v1/audio with a fake yt-dlp (audit #31), no YouTube:
#   sh tests/test_fetch_audio.sh <fetch image> [code dir]
# - yt-dlp failing after the first bytes ends the transfer as an error, not as a complete HTTP 200
# - after a bot check the audio route rests like the others: 429 without starting yt-dlp
set -eu
IMG=$1; CODE=${2:-$(cd "$(dirname "$0")/.." && pwd)/services/fetch}
T=$(mktemp -d); chmod 777 "$T"
cat > "$T/yt-dlp" <<'EOF'
#!/bin/sh
echo run >> /tmp/fake/runs
for a in "$@"; do case "$a" in --version) echo 2099.1.1; exit 0 ;; esac; done
ext=""; prev=""; for a in "$@"; do [ "$prev" = before_dl:%\(ext\)s ] && ext="$a"; prev="$a"; done
[ -n "$ext" ] && echo m4a > "$ext"
head -c 200000 /dev/zero                  # the first bytes of the audio ...
echo "ERROR: [youtube] x: Sign in to confirm you're not a bot" >&2
exit 1                                    # ... then a bot check
EOF
chmod +x "$T/yt-dlp"
docker rm -f fetchtest >/dev/null 2>&1 || true
docker run -d --name fetchtest -e API_KEY=k -e PATH=/tmp/fake:/usr/local/bin:/usr/bin:/bin -e FETCH_MIN_INTERVAL_S=0 \
  -p 127.0.0.1:18784:8000 -v "$T:/tmp/fake" -v "$CODE/app.py:/app/app.py:ro" "$IMG" >/dev/null
trap 'docker rm -f fetchtest >/dev/null 2>&1; rm -rf "$T"' EXIT
i=0; until curl -sf 127.0.0.1:18784/health >/dev/null; do i=$((i+1)); [ $i -lt 60 ] || exit 1; sleep 1; done
: > "$T/runs"
rc=0; curl -sS -o /dev/null -H "Authorization: Bearer k" -H 'Content-Type: application/json' \
  127.0.0.1:18784/v1/audio -d '{"url":"https://www.youtube.com/watch?v=aaaaaaaaaaa"}' 2>/dev/null || rc=$?
[ "$rc" != 0 ] || { echo "FAIL: a stream cut by a yt-dlp failure looked complete (HTTP 200, no transfer error)"; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer k" -H 'Content-Type: application/json' \
  127.0.0.1:18784/v1/audio -d '{"url":"https://www.youtube.com/watch?v=bbbbbbbbbbb"}')
[ "$code" = 429 ] || { echo "FAIL: audio after a bot check: $code (expected 429)"; exit 1; }
[ "$(wc -l < "$T/runs")" = 1 ] || { echo "FAIL: yt-dlp started $(wc -l < "$T/runs") times (the rest must not start it)"; exit 1; }
echo "FETCH AUDIO OK"
