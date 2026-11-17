# node-translate (CPU: python slim) and node-translate-cuda (BASE_IMAGE = llama.cpp server-cuda, pinned by digest:
# EuroLLM for EN->UK on GPU nodes with >= 12 GB VRAM, llm.py). The Opus-MT/NLLB/punctuation code is the same in both.
ARG BASE_IMAGE=python:3.12.12-slim
FROM ${BASE_IMAGE}
USER root
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1
RUN if ! python3 -m pip --version >/dev/null 2>&1; then apt-get update \
      && apt-get install -y --no-install-recommends python3-pip && rm -rf /var/lib/apt/lists/*; fi
RUN python3 -m pip install --no-cache-dir --break-system-packages ctranslate2==4.9.0 tokenizers==0.23.3 \
    huggingface_hub==2.4.0 py3langid==0.4.0 sentencepiece==0.2.1 fastapi==0.162.0 uvicorn==0.54.0
WORKDIR /app
# onnxruntime: punctuation model (punct.py); own layer keeps the one above cached
RUN python3 -m pip install --no-cache-dir --break-system-packages --timeout 120 --retries 10 onnxruntime==1.22.1
COPY app.py worker.py punct.py llm.py ./
RUN mkdir -p /cache && chown nobody /cache
USER nobody
EXPOSE 8000
ENTRYPOINT []
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
