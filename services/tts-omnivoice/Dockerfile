# OmniVoice (k2-fsa) TTS through omnivoice.cpp (MIT, GGML port): its OpenAI-compatible tts-server, one GPU context.
# VARIANT=cuda (node-tts-omnivoice-cuda, GPU nodes) | cpu (local tests only: ~40x slower than real time).
# Weights are not in the image: fetch-weights.sh puts the pinned GGUFs into the omnivoice-models volume.
ARG VARIANT=cuda
FROM nvidia/cuda:12.8.1-devel-ubuntu24.04 AS build-cuda
FROM ubuntu:24.04 AS build-cpu
FROM build-${VARIANT} AS build
ARG VARIANT
ARG OMNIVOICE_COMMIT=a650381a67905fd6ad08b323ed026b042a07b623
RUN apt-get update && apt-get install -y --no-install-recommends build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY patches /patches
# num_step / guidance_scale per request (patches/): the upstream server takes them only on the CLI
RUN git clone -q https://github.com/ServeurpersoCom/omnivoice.cpp.git /src && cd /src && git checkout -q $OMNIVOICE_COMMIT \
    && git submodule update --init --recursive -q && git apply /patches/*.patch \
    && if [ "$VARIANT" = cuda ]; then \
         set -- -DGGML_CUDA=ON -DGGML_CUDA_NCCL=OFF -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
                "-DCMAKE_CUDA_ARCHITECTURES=75;86;89;120" \
                -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined -DCMAKE_SHARED_LINKER_FLAGS=-Wl,--allow-shlib-undefined; \
       else set --; fi \
    && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release "$@" >/dev/null \
    && cmake --build build --config Release -j "$(nproc)" --target tts-server \
    && mkdir -p /out/lib && cp build/tts-server /out/ && cp -P build/*.so* /out/lib/ \
    && if [ "$VARIANT" = cuda ]; then cp -P /usr/local/cuda/lib64/libcublas.so.12* /usr/local/cuda/lib64/libcublasLt.so.12* /out/lib/; fi

FROM nvidia/cuda:12.8.1-base-ubuntu24.04 AS run-cuda
FROM ubuntu:24.04 AS run-cpu
FROM run-${VARIANT}
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/tts-server /usr/local/bin/
COPY --from=build /out/lib/ /usr/local/lib/omnivoice/
COPY fetch-weights.sh run.sh NOTICE /app/
ENV LD_LIBRARY_PATH=/usr/local/lib/omnivoice OMNIVOICE_DIR=/models OMNIVOICE_QUANT=Q8_0 OMNIVOICE_LANG=Ukrainian
# every library must resolve inside the image; only libcuda.so.1 comes from the host driver at run time
# (NCCL is off above: one GPU, and the runtime image has no libnccl)
RUN missing=$(ldd /usr/local/bin/tts-server /usr/local/lib/omnivoice/*.so* 2>/dev/null | grep "not found" | grep -v "libcuda.so.1" | sort -u); \
    if [ -n "$missing" ]; then echo "unresolved libraries:"; echo "$missing"; exit 1; fi
RUN mkdir -p /models && chown 65534:65534 /models
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s CMD curl -fs http://127.0.0.1:8080/health || exit 1
CMD ["/app/run.sh"]
