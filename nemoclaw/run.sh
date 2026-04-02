#!/usr/bin/env bash
set -euo pipefail

log() { printf "[nemoclaw] %s\n" "$*"; }

log "NemoClaw starting — OpenClaw + Portfolio Skills"

# ── Config ─────────────────────────────────────────────────────────

BASE_DIR=/config/nemoclaw
STATE_DIR="${BASE_DIR}/.openclaw"
CONFIG_PATH="${STATE_DIR}/openclaw.json"
WORKSPACE_DIR="${BASE_DIR}/workspace"
PNPM_HOME="${BASE_DIR}/.local/share/pnpm"
SKILLS_DIR="${STATE_DIR}/skills"

mkdir -p "${BASE_DIR}" "${STATE_DIR}" "${WORKSPACE_DIR}" "${PNPM_HOME}" "${SKILLS_DIR}"
mkdir -p "${BASE_DIR}/.config/gh" "${BASE_DIR}/.local" "${BASE_DIR}/.cache" "${BASE_DIR}/.npm" "${BASE_DIR}/bin"

# Symlink /root dirs to persistent storage
for dir in .config .local .cache .npm; do
  target="${BASE_DIR}/${dir}"
  link="/root/${dir}"
  if [ -L "${link}" ]; then :
  elif [ -d "${link}" ]; then
    cp -rn "${link}/." "${target}/" 2>/dev/null || true
    rm -rf "${link}"
    ln -s "${target}" "${link}"
  else
    rm -f "${link}" 2>/dev/null || true
    ln -s "${target}" "${link}"
  fi
done

export HOME="${BASE_DIR}"
export PNPM_HOME="${PNPM_HOME}"
export PATH="${BASE_DIR}/bin:${PNPM_HOME}:${PATH}"
export CI=true
export OPENCLAW_STATE_DIR="${STATE_DIR}"
export OPENCLAW_CONFIG_PATH="${CONFIG_PATH}"

# ── Read HA options ────────────────────────────────────────────────

OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    export TELEGRAM_BOT_TOKEN=$(jq -r '.telegram_bot_token' "$OPTIONS_FILE")
    export TELEGRAM_CHAT_ID=$(jq -r '.telegram_chat_id' "$OPTIONS_FILE")
    export TELEGRAM_USER_ID=$(jq -r '.telegram_user_id' "$OPTIONS_FILE")
    export VLLM_BASE_URL=$(jq -r '.vllm_base_url' "$OPTIONS_FILE")
    export VLLM_MODEL=$(jq -r '.vllm_model' "$OPTIONS_FILE")
    export PORTFOLIO_DB_PATH=$(jq -r '.portfolio_db_path' "$OPTIONS_FILE")
    export TZ=$(jq -r '.timezone' "$OPTIONS_FILE")
    log "Options loaded"
fi

# ── Install portfolio skills into OpenClaw ─────────────────────────

log "Installing portfolio skills..."
cp -r /opt/nemoclaw/skills/* "${SKILLS_DIR}/" 2>/dev/null || true
log "Skills installed at ${SKILLS_DIR}"

# ── Setup OpenClaw ─────────────────────────────────────────────────

INSTALL_MODE="$(jq -r '.install_mode // "package"' /data/options.json 2>/dev/null || true)"
[ -z "${INSTALL_MODE}" ] || [ "${INSTALL_MODE}" = "null" ] && INSTALL_MODE="package"
log "install_mode=${INSTALL_MODE}"

REPO_URL="$(jq -r '.repo_url // empty' /data/options.json 2>/dev/null || true)"
BRANCH="$(jq -r '.branch // ""' /data/options.json 2>/dev/null || true)"
TOKEN_OPT="$(jq -r '.github_token // ""' /data/options.json 2>/dev/null || true)"

REPO_DIR="${BASE_DIR}/openclaw-src"

if [ "${INSTALL_MODE}" = "source" ]; then
  if [ -z "${REPO_URL}" ] || [ "${REPO_URL}" = "null" ]; then
    log "repo_url empty; set in options"
    exit 1
  fi
  if [ -n "${TOKEN_OPT}" ] && [ "${TOKEN_OPT}" != "null" ]; then
    REPO_URL="https://${TOKEN_OPT}@${REPO_URL#https://}"
  fi
  if [ ! -d "${REPO_DIR}/.git" ]; then
    log "cloning ${REPO_URL}"
    rm -rf "${REPO_DIR}"
    if [ -n "${BRANCH}" ] && [ "${BRANCH}" != "null" ]; then
      git clone --branch "${BRANCH}" "${REPO_URL}" "${REPO_DIR}"
    else
      git clone "${REPO_URL}" "${REPO_DIR}"
    fi
  else
    log "updating repo"
    git -C "${REPO_DIR}" fetch --prune
    target_branch="${BRANCH:-$(git -C "${REPO_DIR}" remote show origin | sed -n '/HEAD branch/s/.*: //p')}"
    git -C "${REPO_DIR}" checkout "${target_branch}" 2>/dev/null || true
    git -C "${REPO_DIR}" reset --hard "origin/${target_branch}"
  fi
  cd "${REPO_DIR}"
  pnpm install --no-frozen-lockfile --prefer-frozen-lockfile --prod=false
  pnpm build
  cat > "${BASE_DIR}/bin/openclaw" <<'EOF_WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
exec node "/config/nemoclaw/openclaw-src/openclaw.mjs" "$@"
EOF_WRAPPER
  chmod +x "${BASE_DIR}/bin/openclaw"
fi

if [ ! -f "${CONFIG_PATH}" ]; then
  openclaw setup --workspace "${WORKSPACE_DIR}"
else
  log "OpenClaw config exists"
fi

# Ensure gateway auth token
if [ -f "${CONFIG_PATH}" ]; then
  if ! jq -e '(.gateway.auth.token // "") | tostring | length > 0' "${CONFIG_PATH}" >/dev/null 2>&1; then
    token="$(node -e "process.stdout.write(require('crypto').randomBytes(24).toString('hex'))")"
    tmp="$(mktemp)"
    jq --arg t "${token}" '.gateway = (.gateway // {}) | .gateway.auth = (.gateway.auth // {}) | .gateway.auth.token = $t | .gateway.auth.mode = (.gateway.auth.mode // "token")' "${CONFIG_PATH}" > "${tmp}" && mv "${tmp}" "${CONFIG_PATH}"
    log "gateway auth token set"
  fi
fi

# ── Start portfolio agent in background ────────────────────────────

log "Starting portfolio agent (price updates, alerts, snapshots)..."
PYTHONPATH=/opt/nemoclaw/agent python3 -m nemoclaw.main &
AGENT_PID=$!
log "Agent PID: ${AGENT_PID}"

# ── Start OpenClaw gateway (foreground) ────────────────────────────

PORT="$(jq -r '.port // 18790' /data/options.json)"
VERBOSE="$(jq -r '.verbose // false' /data/options.json)"

ARGS=(gateway --port "${PORT}")
[ "${VERBOSE}" = "true" ] && ARGS+=(--verbose)

if [ ! -f "${CONFIG_PATH}" ] || ! jq -e '(.gateway.mode // "") | length > 0' "${CONFIG_PATH}" >/dev/null 2>&1; then
  ARGS+=(--allow-unconfigured)
fi

# Kill any leftover gateway from previous runs
openclaw gateway stop 2>/dev/null || true
sleep 2

log "Starting OpenClaw gateway on port ${PORT}..."
openclaw "${ARGS[@]}" &
GATEWAY_PID=$!
log "OpenClaw gateway PID: ${GATEWAY_PID}"

# Keep container alive — wait for either process
wait -n ${AGENT_PID} ${GATEWAY_PID} 2>/dev/null || true

# If we get here, one process died. Keep the other running.
log "A process exited. Keeping container alive..."
while true; do sleep 60; done
