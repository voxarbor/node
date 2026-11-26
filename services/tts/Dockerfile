ARG BASE_IMAGE=ghcr.io/voxarbor/node-base-torch:2.11.0-cpu-1
FROM ${BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10 \
    HF_HOME=/cache/hf STANZA_RESOURCES_DIR=/cache/stanza GRADIO_ANALYTICS_ENABLED=False \
    VOICES_DIR=/opt/space/voices SINGLE_STYLE=/opt/space/filatov.pt
RUN apt-get update && apt-get install -y --no-install-recommends git git-lfs && rm -rf /var/lib/apt/lists/*  # ffmpeg: base
# torch comes from the base: node-base-torch:<ver>-cpu here, -cu128 for node-tts-cuda (BASE_IMAGE build arg)
COPY requirements.txt /tmp/
RUN pip install -r /tmp/requirements.txt \
    && pip install --no-deps git+https://github.com/patriotyk/ukrainian-word-stress@d5b37ea0abe9711930d9c3b0b13edec5a0675512
ENV HF_HUB_DISABLE_XET=1 HOLOS_DIR=/opt/holos ACCENTUK_ASSETS=/opt/accentuk
COPY requirements-engines.txt /tmp/
# pip cache mount: PyPI downloads from this host time out now and then, a retried build resumes from the cache
RUN --mount=type=cache,target=/root/.cache/pip PIP_NO_CACHE_DIR=0 pip install -r /tmp/requirements-engines.txt \
    && PIP_NO_CACHE_DIR=0 pip install --no-deps holos-tts==0.4
# HolosTTS PyTorch FP32 weights (the only build: quality first) + the voice encoder (custom voices), accentuk assets (stress lexicon, CT2 INT8 models, spaCy uk_core_news_sm)
# live in the image: small, read-only and needed offline. (Xet downloads from HF hang/fail here: disabled.)
ARG HOLOS_REVISION=5d40fbaa99e55ebd3359a161bc86728ffdbd5c5f
ENV HOLOS_REVISION=${HOLOS_REVISION}
RUN python -c "from huggingface_hub import snapshot_download as d; d('patriotyk/HolosTTS', revision='$HOLOS_REVISION', local_dir='/opt/holos', allow_patterns=['holos.pt', 'voices.pt', 'voice_encoder.pt', 'LICENSE'])" \
    && python -c "import torch, json; v = torch.load('/opt/holos/voices.pt', map_location='cpu'); json.dump(list(v['names'] if isinstance(v, dict) and 'names' in v else v), open('/opt/holos/voices.json', 'w'), ensure_ascii=False)" \
    && rm -rf /opt/holos/.cache && accentuk download --light --tts --directory /opt/accentuk
# Voice style vectors (*.pt, git-lfs) from the HF Space, pinned to a commit.
ARG SPACE_COMMIT=a292a2841d995cc6efc62f9d22fa8459d62c784f
RUN git clone -q https://huggingface.co/spaces/patriotyk/styletts2-ukrainian /opt/space && cd /opt/space \
    && git checkout -q $SPACE_COMMIT && git lfs pull && rm -rf .git \
    && python -c "import torch,glob; [torch.load(p) for p in glob.glob('/opt/space/voices/*.pt')]"
# espeak-ng: English name phonetics for /v1/text/phonemize (own layer: keeps the heavy layers above cached)
RUN apt-get update && apt-get install -y --no-install-recommends espeak-ng=1.52.0+dfsg-5 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /cache /jobs /config && chown nobody:nogroup /cache /jobs /config
WORKDIR /app
COPY app.py worker.py ui.py lexicon.py phonemize.py speech.py jobs.py lexicon.seed.txt ./
COPY web ./web
USER nobody
VOLUME /cache
EXPOSE 8000 8001
CMD ["python", "app.py"]
