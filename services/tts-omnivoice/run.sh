#!/bin/sh
# weights first (idempotent), then the server; voices live only in its RAM: node-tts registers them again on demand
set -eu
/app/fetch-weights.sh
q=${OMNIVOICE_QUANT:-Q8_0}
dev=${OMNIVOICE_DEVICE:+--device $OMNIVOICE_DEVICE}
# shellcheck disable=SC2086
exec tts-server --model "$OMNIVOICE_DIR/omnivoice-base-$q.gguf" --codec "$OMNIVOICE_DIR/omnivoice-tokenizer-$q.gguf" \
  --host 0.0.0.0 --port 8080 --lang "${OMNIVOICE_LANG:-Ukrainian}" $dev
