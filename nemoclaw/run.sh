#!/bin/bash
set -e

echo "=== NemoClaw Financial Agent Starting ==="

# Read HA add-on options
OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    export TELEGRAM_BOT_TOKEN=$(jq -r '.telegram_bot_token' "$OPTIONS_FILE")
    export TELEGRAM_CHAT_ID=$(jq -r '.telegram_chat_id' "$OPTIONS_FILE")
    export TELEGRAM_USER_ID=$(jq -r '.telegram_user_id' "$OPTIONS_FILE")
    export VLLM_BASE_URL=$(jq -r '.vllm_base_url' "$OPTIONS_FILE")
    export VLLM_MODEL=$(jq -r '.vllm_model' "$OPTIONS_FILE")
    export PORTFOLIO_DB_PATH=$(jq -r '.portfolio_db_path' "$OPTIONS_FILE")
    export TZ=$(jq -r '.timezone' "$OPTIONS_FILE")
    export PRICE_UPDATE_INTERVAL=$(jq -r '.price_update_interval' "$OPTIONS_FILE")
    export DAILY_ANALYSIS_TIME=$(jq -r '.daily_analysis_time' "$OPTIONS_FILE")
    export WEEKLY_REVIEW_DAY=$(jq -r '.weekly_review_day' "$OPTIONS_FILE")
    export WEEKLY_REVIEW_TIME=$(jq -r '.weekly_review_time' "$OPTIONS_FILE")
    echo "Options loaded"
else
    echo "WARNING: No options file found"
fi

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ "$TELEGRAM_BOT_TOKEN" = "null" ]; then
    echo "ERROR: telegram_bot_token is required. Configure it in the add-on settings."
    sleep 10
    exit 1
fi

cd /app

exec python3 -m nemoclaw.main
