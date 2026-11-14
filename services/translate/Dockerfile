FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/cache HF_HUB_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_XET=1
RUN pip install --no-cache-dir ctranslate2==4.9.0 tokenizers==0.23.3 huggingface_hub==2.4.0 py3langid==0.4.0 sentencepiece==0.2.1 \
    fastapi==0.162.0 uvicorn==0.54.0
WORKDIR /app
# onnxruntime: punctuation model (punct.py); own layer keeps the one above cached
RUN pip install --no-cache-dir --timeout 120 --retries 10 onnxruntime==1.22.1
COPY app.py worker.py punct.py ./
RUN mkdir -p /cache && chown nobody /cache
USER nobody
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
