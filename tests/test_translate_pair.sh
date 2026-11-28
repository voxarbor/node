#!/bin/sh
# audit #40: a pair this node cannot translate (no Opus-MT model, NLLB off) answers 422 with X-Error-Code:
# pair_unavailable, so the coordinator tries another node; a bad request keeps a plain 422.
#   sh tests/test_translate_pair.sh <translate image> [code dir]   (needs the Opus models volume voxarbor-node_translate-cache
#   or TRANSLATE_CACHE=<volume>)
set -eu
IMG=$1; CODE=${2:-$(cd "$(dirname "$0")/.." && pwd)/services/translate}
VOL=${TRANSLATE_CACHE:-voxarbor-node_translate-cache}
docker rm -f trpair >/dev/null 2>&1 || true
docker run -d --name trpair -e API_KEY=k -e TRANSLATE_NLLB=0 -p 127.0.0.1:18783:8000 -v "$VOL:/cache:ro" \
  -v "$CODE:/app:ro" "$IMG" >/dev/null
trap 'docker rm -f trpair >/dev/null 2>&1' EXIT
i=0; until curl -sf 127.0.0.1:18783/health >/dev/null; do i=$((i+1)); [ $i -lt 60 ] || exit 1; sleep 1; done
h=$(curl -s -D - -o /dev/null -H "Authorization: Bearer k" -H 'Content-Type: application/json' 127.0.0.1:18783/v1/translate \
     -d '{"segments":["こんにちは"],"source":"ja","target":"uk"}')
echo "$h" | grep -q "^HTTP/1.1 422" || { echo "FAIL: ja->uk without NLLB: $h"; exit 1; }
echo "$h" | grep -qi "^x-error-code: pair_unavailable" || { echo "FAIL: no X-Error-Code pair_unavailable"; exit 1; }
h=$(curl -s -D - -o /dev/null -H "Authorization: Bearer k" -H 'Content-Type: application/json' 127.0.0.1:18783/v1/translate \
     -d '{"segments":["x"],"source":"en","target":"uk","settings":{"beam_size":99}}')
echo "$h" | grep -qi "x-error-code" && { echo "FAIL: a bad request is marked pair_unavailable"; exit 1; }
echo "PAIR OK"
