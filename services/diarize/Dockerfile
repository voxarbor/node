FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl bzip2 && rm -rf /var/lib/apt/lists/*
# Models from the sherpa-onnx GitHub releases (no Hugging Face token): pyannote segmentation-3.0 (MIT) and the
# WeSpeaker ResNet34 VoxCeleb speaker embedding (Apache-2.0); checksums pinned.
RUN mkdir -p /models/segmentation && cd /models \
    && curl -fsSL --retry 10 --retry-all-errors -o seg.tar.bz2 https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2 \
    && echo "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488  seg.tar.bz2" | sha256sum -c \
    && tar xjf seg.tar.bz2 && mv sherpa-onnx-pyannote-segmentation-3-0/model.onnx sherpa-onnx-pyannote-segmentation-3-0/LICENSE segmentation/ \
    && rm -rf seg.tar.bz2 sherpa-onnx-pyannote-segmentation-3-0 \
    && curl -fsSL --retry 10 --retry-all-errors -o embedding.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx \
    && echo "e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012  embedding.onnx" | sha256sum -c
RUN pip install --no-cache-dir --timeout 120 --retries 10 sherpa-onnx==2.0.1 soundfile==0.13.1 numpy==2.2.6 \
    fastapi==0.162.0 uvicorn==0.54.0 python-multipart==0.0.33
WORKDIR /app
COPY app.py worker.py gender_lr.npz ./
USER nobody
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
