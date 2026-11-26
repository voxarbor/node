# VoxArbor Node /gen: Remotion Render (model "remotion", kind render). Node 22, @remotion/renderer 4.0.545 with its own
# compositor and ffmpeg, Chrome Headless Shell (downloaded at build) and the libraries it needs; Vulkan / EGL loaders
# for gl=vulkan|angle|egl on GPU nodes (the NVIDIA driver libraries come from the container toolkit at run time).
FROM node:22.23.5-bookworm-slim
ENV NODE_ENV=production HOME=/tmp
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates unzip \
      libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libasound2 libxrandr2 \
      libxkbcommon0 libxfixes3 libxcomposite1 libxdamage1 libxext6 libx11-6 libxcb1 libpango-1.0-0 libcairo2 \
      libexpat1 libglib2.0-0 fonts-dejavu-core fonts-noto-color-emoji libvulkan1 libegl1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund \
    && for i in 1 2 3; do node -e "import('@remotion/renderer').then(r => r.ensureBrowser())" && break; sleep 10; done \
    && chmod -R a+rX /app/node_modules \
    && shell=$(find /app/node_modules -type f -name chrome-headless-shell | head -1) && test -x "$shell" \
    && missing=$(ldd "$shell" | grep "not found" || true) && if [ -n "$missing" ]; then echo "$missing"; exit 1; fi
COPY VERSION server.mjs render.mjs ./
RUN mkdir -p /cache && chown 65534:65534 /cache
USER 65534:65534
EXPOSE 8000
CMD ["node", "server.mjs"]
