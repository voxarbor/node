#!/bin/sh
# Pinned OmniVoice GGUF weights (Serveurperso/OmniVoice-GGUF@0170941, CC-BY-NC-4.0, see NOTICE) into $OMNIVOICE_DIR,
# checked by sha256; files already present and correct are kept. OMNIVOICE_QUANT=Q8_0 (default) | BF16.
set -eu
REV=017094167b5c9ed565a5076ac9b3b93c5ecf5c73
DIR=${OMNIVOICE_DIR:-/models}
case "${OMNIVOICE_QUANT:-Q8_0}" in
  Q8_0) set -- omnivoice-base-Q8_0.gguf 2882d887921798aea13d45236556bdf8012842ab6f8cd2690943eead6289f298 \
               omnivoice-tokenizer-Q8_0.gguf 75204fa566a8e30984e7a1066da6557184c9fd099c8f1bc0cb5b9415edfec255 ;;
  BF16) set -- omnivoice-base-BF16.gguf c4d2e4e6506a88f9c9900621606470bca6a523c72819bf4a5e5dac80961075bf \
               omnivoice-tokenizer-BF16.gguf c2179e4cf528b19fea22a5be94c34c083877bb5fc28ac0245d2b4299a262dcec ;;
  *) echo "OMNIVOICE_QUANT: Q8_0 or BF16" >&2; exit 2 ;;
esac
while [ $# -gt 0 ]; do
  f=$1 sum=$2; shift 2
  if [ -s "$DIR/$f" ] && echo "$sum  $DIR/$f" | sha256sum -c --status; then echo "$f present"; continue; fi
  echo "downloading $f"
  curl -fL --retry 10 --retry-all-errors -o "$DIR/$f.part" "https://huggingface.co/Serveurperso/OmniVoice-GGUF/resolve/$REV/$f"
  echo "$sum  $DIR/$f.part" | sha256sum -c --status || { echo "$f: sha256 mismatch" >&2; rm -f "$DIR/$f.part"; exit 1; }
  mv "$DIR/$f.part" "$DIR/$f"
done
