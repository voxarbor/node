# Common base of the audio services (tts, diarize): Python + ffmpeg, so a node stores these layers once.
FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=300 PIP_RETRIES=10 \
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache PYTORCH_KERNEL_CACHE_PATH=/tmp/.cache/torch-kernels
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
