#!/bin/sh
# Whole run on a GPU node (README.md): [replay] -> prep -> train -> convert to CT2 int8 -> eval. Needs docker + GPU.
#   sh run.sh <data dir> <work dir>
# env: OLD_CT2 (the en-zle folder the nodes run now), LR, EPOCHS, BATCH, AUTO=1 (add train_auto.tsv),
#      REPLAY_N (make replay.tsv: N WikiText sentences translated by the old model), REPLAY_RATIO (replay per own pair)
set -eu
DATA=$(cd "$1" && pwd); WORK=$(mkdir -p "$2" && cd "$2" && pwd); HERE=$(cd "$(dirname "$0")" && pwd)
IMG=ghcr.io/voxarbor/node-base-torch:2.8.0-cu128-1
PIP="transformers==4.57.1 datasets==3.6.0 sentencepiece==0.2.1 sacremoses==0.1.1 sacrebleu==2.5.1 ctranslate2==4.9.0 accelerate==1.10.1"
REV=${BASE_REVISION:-main}
run() { docker run --rm --gpus all -e HF_HOME=/work/hf -e HF_HUB_DISABLE_XET=1 -e BASE_REVISION=$REV -e LR=${LR:-2e-5} \
  -e EPOCHS=${EPOCHS:-6} -e BATCH=${BATCH:-16} -e AUTO=${AUTO:-0} -e REPLAY_RATIO=${REPLAY_RATIO:-} -e REPLAY=/work/replay.tsv \
  -v "$DATA":/data:ro -v "$WORK":/work -v "$HERE":/kit:ro -w /work \
  --entrypoint sh $IMG -c "pip install -q --no-cache-dir $PIP >/dev/null 2>&1 && $CULIB && $1"; }
# CTranslate2 on the GPU (replay.py) needs libcublas.so.12: it is in torch's pip nvidia packages, not on the path
CULIB='export LD_LIBRARY_PATH=$(python -c "import glob,site;print(\":\".join(glob.glob(site.getsitepackages()[0]+\"/nvidia/*/lib\")))"):${LD_LIBRARY_PATH:-}'
# old = what the nodes run now: OLD_CT2=<the en-zle folder from the translate-cache volume>, else converted from HF
if [ -n "${OLD_CT2:-}" ]; then rm -rf "$WORK/ct2-old"; cp -r "$OLD_CT2" "$WORK/ct2-old"; else
  run "ct2-transformers-converter --model Helsinki-NLP/opus-mt-tc-big-en-zle --revision $REV --quantization int8 --output_dir /work/ct2-old --copy_files source.spm target.spm --force"; fi
if [ -n "${REPLAY_N:-}" ] && [ ! -s "$DATA/replay.tsv" ] && [ ! -s "$WORK/replay.tsv" ]; then
  run "python /kit/replay.py /work/ct2-old /work/replay.tsv $REPLAY_N"; fi
run "python /kit/prep.py /data /work/prep"
run "python /kit/train.py /work/prep /work/model 2>&1 | tee /work/train.log | grep -E 'eval_loss|best checkpoint|Error' "
run "ct2-transformers-converter --model /work/model/best --quantization int8 --output_dir /work/ct2-new --copy_files source.spm target.spm --force"
run "python /kit/eval.py /work/prep /work/ct2-old /work/ct2-new /work/eval"
echo "done: $WORK/eval/report.json, blind judge files in $WORK/eval/judge, new model in $WORK/ct2-new"
