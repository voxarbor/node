FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp XDG_CACHE_HOME=/tmp/cache
# yt-dlp with its default extras: yt-dlp-ejs + deno solve YouTube's JS challenges. yt-dlp goes stale fast:
# bump these pins (and services/fetch/VERSION) whenever YouTube breaks extraction.
RUN pip install --no-cache-dir --timeout 120 --retries 10 "yt-dlp[default]==2026.11.23" yt-dlp-ejs==0.8.0 deno==2.9.8 \
    fastapi==0.162.0 uvicorn==0.54.0
WORKDIR /app
COPY app.py ./
USER nobody
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
