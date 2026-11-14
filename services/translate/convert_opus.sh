#!/bin/sh
# One-off (torch CPU wheel pre-downloaded to ./.wheels, mounted at /wheels): convert Opus-MT tc-big models (X -> East Slavic, ">>ukr<<" target token) to CTranslate2 int8 into the
# translate-cache volume. Run: docker run --rm -v <translate cache volume>:/cache python:3.12.12-slim sh /convert_opus.sh
set -e
pip install -q --no-cache-dir --timeout 120 --retries 10 /wheels/torch-2.9.1+cpu-cp312-cp312-manylinux_2_28_x86_64.whl
pip install -q --no-cache-dir --timeout 120 --retries 10 ctranslate2==4.9.0 transformers==4.57.1 sentencepiece==0.2.1 sacremoses==0.1.1
export HF_HUB_DISABLE_XET=1 HF_HOME=/tmp/hf
for pair in en-zle de-zle zle-zle zlw-zle fr-zle es-zle it-zle pt-zle; do
  out=/cache/opus-mt/$pair
  [ -f $out/model.bin ] && continue
  ct2-transformers-converter --model Helsinki-NLP/opus-mt-tc-big-$pair --quantization int8 --output_dir $out \
    --copy_files source.spm target.spm
  rm -rf /tmp/hf
done
chown -R 65534 /cache/opus-mt; du -sh /cache/opus-mt/*
