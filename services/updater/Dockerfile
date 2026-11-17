# node-updater: updates this node to a signed GitHub release of voxarbor/node (docs/updater.md). The only service with
# the docker socket. Pinned tools: docker CLI (static), compose plugin, cosign; checksums verified at build time.
FROM python:3.12.12-slim
ARG DOCKER_VERSION=29.10.0
ARG DOCKER_SHA256=7c1ebadc65c716cdc6d35fc78759ed0e4317debf43c21dd12f066f2006999def
ARG COMPOSE_VERSION=v5.7.1
ARG COMPOSE_SHA256=af426722a8af5de812fcee30cc1c39f0639f5f723cbadefb25e5d4db054064cf
ARG COSIGN_VERSION=v3.1.4
ARG COSIGN_SHA256=424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL --retry 10 --retry-all-errors -o /tmp/d.tgz https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz \
    && echo "${DOCKER_SHA256}  /tmp/d.tgz" | sha256sum -c \
    && tar xzf /tmp/d.tgz -C /tmp docker/docker && mv /tmp/docker/docker /usr/local/bin/docker && rm -rf /tmp/d.tgz /tmp/docker \
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL --retry 10 --retry-all-errors -o /usr/local/lib/docker/cli-plugins/docker-compose \
       https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64 \
    && echo "${COMPOSE_SHA256}  /usr/local/lib/docker/cli-plugins/docker-compose" | sha256sum -c \
    && curl -fsSL --retry 10 --retry-all-errors -o /usr/local/bin/cosign https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-amd64 \
    && echo "${COSIGN_SHA256}  /usr/local/bin/cosign" | sha256sum -c \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose /usr/local/bin/cosign
ARG VOICE_CORE_RELEASE=dev
ENV UPDATER_RELEASE=${VOICE_CORE_RELEASE} HOME=/tmp PYTHONUNBUFFERED=1
WORKDIR /app
COPY updater.py VERSION ./
# root: the docker socket's group differs between hosts (Linux, WSL, Docker Desktop)
CMD ["python", "updater.py"]
