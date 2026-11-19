FROM python:3.12.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# the VoxArbor Node release this image belongs to (CI build arg)
ARG VOXARBOR_NODE_RELEASE=dev
ENV VOXARBOR_NODE_RELEASE=${VOXARBOR_NODE_RELEASE}
WORKDIR /app
COPY app.py init_models.py ./
USER nobody
EXPOSE 8000
CMD ["python", "app.py"]
