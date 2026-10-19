# syntax=docker/dockerfile:1
# The voice engine (vpengine) and the vk CLI in one image. The web app is prebuilt
# in engine/src/vpengine/static, so no Node is needed. Build it yourself with
#   scripts/docker-up.sh            (or: docker compose up -d --build)
# Licences: see NOTICE. The Kokoro speech extra installs GPL-3.0 packages
# (phonemizer, espeak-ng); if you publish this image, follow their terms.

FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1: onnxruntime and CTranslate2 (Whisper); tini: clean signals and no zombies.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/vpe
COPY cli/pyproject.toml cli/README.md cli/
COPY cli/src cli/src
COPY engine/pyproject.toml engine/README.md engine/
COPY engine/src engine/src
RUN pip install ./cli ./engine && rm -rf /opt/vpe/cli /opt/vpe/engine

COPY docker/entrypoint.sh /usr/local/bin/vpe-entrypoint
RUN chmod 755 /usr/local/bin/vpe-entrypoint && mkdir -p /agents /data && chmod 777 /agents /data

# Everything the engine keeps lives under /data (settings with your keys, records, knowledge,
# downloaded models); your agents live in /agents. Both are mounted from the host.
ENV VPE_CONTAINER=1 \
    VPE_HOST=0.0.0.0 \
    VPE_PORT=7860 \
    VPE_AGENTS_DIR=/agents \
    VPE_SETTINGS_FILE=/data/config/settings.json \
    VPE_DATA_DIR=/data/library \
    VPE_MODEL_CACHE=/data/cache \
    HOME=/data

EXPOSE 7860
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/v1/health', timeout=4)" || exit 1
ENTRYPOINT ["tini", "--", "vpe-entrypoint"]
