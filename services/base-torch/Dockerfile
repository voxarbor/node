# node-base-av + PyTorch: one torch layer shared by the images built on it (tts and diarize on a GPU node, tts on a
# CPU node). TORCH_VARIANT = cpu | cu128 (CUDA 12.8 wheels, Blackwell included).
ARG BASE_AV=ghcr.io/voxarbor/node-base-av:1
FROM ${BASE_AV}
ARG TORCH_VARIANT=cpu
RUN pip install --index-url https://download.pytorch.org/whl/${TORCH_VARIANT} torch==2.11.0 torchaudio==2.11.0
