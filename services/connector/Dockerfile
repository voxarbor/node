FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir --timeout 120 --retries 10 websockets==17.4 aiohttp==3.14.5
ARG VOICE_CORE_RELEASE=dev
ENV VOICE_CORE_RELEASE=${VOICE_CORE_RELEASE}
WORKDIR /app
COPY app.py VERSION ./
RUN mkdir -p /state && chown nobody:nogroup /state && sed -i "s/^VERSION = .*/VERSION = \"$(cat VERSION)\"/" app.py
USER nobody
VOLUME /state
EXPOSE 8000
CMD ["python", "app.py"]
