FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app.py init_models.py ./
USER nobody
EXPOSE 8000
CMD ["python", "app.py"]
