FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# the voice-core release this image belongs to (CI build arg): reported even when a node pins VOICE_CORE_TAG=latest
ARG VOICE_CORE_RELEASE=dev
ENV VOICE_CORE_RELEASE=${VOICE_CORE_RELEASE}
WORKDIR /app
COPY app.py init_models.py ./
USER nobody
EXPOSE 8000
CMD ["python", "app.py"]
