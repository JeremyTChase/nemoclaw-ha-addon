FROM node:22-bookworm-slim

ARG BUILD_ARCH
ARG BUILD_VERSION

LABEL io.hass.type="addon" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.version="${BUILD_VERSION}"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# System deps
RUN apt-get update \
  && apt-get upgrade -y \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    jq \
    ripgrep \
    curl \
    nano \
    unzip \
    python3 \
    python3-pip \
    python3-venv \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# Node package managers + OpenClaw
ENV PNPM_HOME=/pnpm
ENV PATH="${PNPM_HOME}:${PATH}"

RUN npm install -g pnpm openclaw@2026.3.2 \
  && mkdir -p /pnpm \
  && pnpm config set global-bin-dir /pnpm \
  && pnpm add -g clawdhub

# Python deps for portfolio skills
RUN pip3 install --no-cache-dir --break-system-packages \
  python-telegram-bot \
  yfinance \
  pandas \
  numpy \
  requests \
  feedparser \
  openai \
  schedule \
  fastapi \
  'uvicorn[standard]' \
  pydantic

# Copy portfolio skills and run script
COPY skills/ /opt/jezclaw/skills/
COPY jezclaw/ /opt/jezclaw/agent/jezclaw/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /opt/jezclaw

CMD [ "/run.sh" ]
