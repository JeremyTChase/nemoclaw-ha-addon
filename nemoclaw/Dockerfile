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

RUN npm install -g pnpm openclaw \
  && mkdir -p /pnpm \
  && pnpm config set global-bin-dir /pnpm \
  && pnpm add -g clawdhub

# Python deps for portfolio skills
RUN pip3 install --no-cache-dir --break-system-packages \
  yfinance \
  pandas \
  numpy \
  requests \
  feedparser \
  openai \
  schedule

# Copy portfolio skills and run script
COPY skills/ /opt/nemoclaw/skills/
COPY nemoclaw/ /opt/nemoclaw/agent/nemoclaw/
COPY run.sh /run.sh
RUN chmod +x /run.sh

WORKDIR /opt/nemoclaw

CMD [ "/run.sh" ]
