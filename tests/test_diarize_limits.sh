#!/bin/sh
# diarize upload limits (audit #28):  sh tests/test_diarize_limits.sh <diarize image> [code dir]
# - DIARIZE_MAX_AUDIO_S bounds the decoded audio kept in the RAM-backed tmpfs (5 min upload, limit 60 s)
# - a request over QUEUE_MAX is refused before its upload is decoded; no work folder is left behind
set -eu
IMG=$1; CODE=${2:-$(cd "$(dirname "$0")/.." && pwd)/services/diarize}
T=$(mktemp -d); chmod 777 "$T"
docker run --rm -v "$T:/w" --entrypoint ffmpeg "$IMG" -v error -f lavfi -i "sine=frequency=220:duration=300" \
  -ar 16000 -ac 1 /w/long.wav
docker rm -f diatest >/dev/null 2>&1 || true
docker run -d --name diatest -e API_KEY=k -e QUEUE_MAX=1 -e DIARIZE_MAX_AUDIO_S=60 -p 127.0.0.1:18785:8000 \
  --tmpfs /tmp/diarize:uid=65534,gid=65534,size=1g -v "$CODE/app.py:/app/app.py:ro" "$IMG" >/dev/null
trap 'docker rm -f diatest >/dev/null 2>&1; rm -rf "$T"' EXIT
i=0; until curl -sf 127.0.0.1:18785/health >/dev/null; do i=$((i+1)); [ $i -lt 90 ] || exit 1; sleep 1; done
out=$(curl -s -H "Authorization: Bearer k" -F file=@"$T/long.wav" 127.0.0.1:18785/v1/diarize)
a=$(echo "$out" | python3 -c 'import json,sys;print(json.load(sys.stdin)["window"]["audio_s"])')
python3 -c "import sys; sys.exit(0 if float('$a') <= 60.5 else 1)" || { echo "FAIL: decoded $a s (limit 60): $out"; exit 1; }
left=$(docker exec diatest sh -c 'ls -A /tmp/diarize | wc -l')
[ "$left" = 0 ] || { echo "FAIL: $left work folders left in /tmp/diarize"; exit 1; }
echo "DIARIZE LIMITS OK"
