ARG BASE_IMAGE=ghcr.io/voxarbor/node-base-av:1
FROM ${BASE_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends curl bzip2 && rm -rf /var/lib/apt/lists/*  # ffmpeg: base
# Models from the sherpa-onnx GitHub releases (no Hugging Face token): pyannote segmentation-3.0 (MIT) and the
# WeSpeaker ResNet34 VoxCeleb speaker embedding (Apache-2.0); checksums pinned.
RUN mkdir -p /models/segmentation && cd /models \
    && curl -fsSL --retry 10 --retry-all-errors -o seg.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 \
    && echo "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488  seg.tar.bz2" | sha256sum -c \
    && tar xjf seg.tar.bz2 && mv sherpa-onnx-pyannote-segmentation-3-0/model.onnx sherpa-onnx-pyannote-segmentation-3-0/LICENSE segmentation/ \
    && rm -rf seg.tar.bz2 sherpa-onnx-pyannote-segmentation-3-0 \
    && curl -fsSL --retry 10 --retry-all-errors -o embedding.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx \
    && echo "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012  embedding.onnx" | sha256sum -c
RUN pip install --no-cache-dir --timeout 120 --retries 10 sherpa-onnx==2.0.3 soundfile==0.13.1 numpy==2.2.6 \
    fastapi==0.162.0 uvicorn==0.54.0 python-multipart==0.0.33
# DIARIZE_VARIANT=cuda builds node-diarize-cuda: pyannote speaker-diarization-community-1 (CC-BY-4.0) on
# CUDA 12.8 PyTorch; its weights are fetched at build time with the build secret hf_token (gated on HF, the license
# allows redistribution with attribution, see NOTICE), so nodes need no token. The CPU image stays sherpa-onnx only.
ARG DIARIZE_VARIANT=cpu
ARG PYANNOTE_REVISION=3533c8cf8e369892e6b79ff1bf80f7b0286a54ee
ENV PYANNOTE_REVISION=${PYANNOTE_REVISION}
RUN if [ "$DIARIZE_VARIANT" = cuda ]; then \
      pip install --no-cache-dir --timeout 300 --retries 10 pyannote.audio==4.0.7 \
        torch==2.8.0 torchaudio==2.8.0 torchcodec==0.7.0 numpy==2.2.6; fi  # torch pinned = node-base-torch cu128 layer, no second copy
RUN --mount=type=secret,id=hf_token,required=false if [ "$DIARIZE_VARIANT" = cuda ]; then \
      HF_TOKEN=$(cat /run/secrets/hf_token) python -c "import os; from huggingface_hub import snapshot_download as d; d('pyannote/speaker-diarization-community-1', revision='$PYANNOTE_REVISION', local_dir='/models/community-1', token=os.environ['HF_TOKEN'])" \
      && rm -rf /models/community-1/.cache /models/community-1/*.gif /tmp/.cache && ls /models/community-1; fi  # /tmp/.cache: root-owned otherwise, the service (nobody) needs it
WORKDIR /app
COPY app.py worker.py gender_lr.npz ./
USER nobody
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
