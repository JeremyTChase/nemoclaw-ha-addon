"""Configuration from environment variables (set by HA add-on options)."""

import os


def get(key, default=None):
    return os.environ.get(key, default)


TELEGRAM_BOT_TOKEN = get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(get("TELEGRAM_CHAT_ID", "0"))
TELEGRAM_USER_ID = int(get("TELEGRAM_USER_ID", "0"))
VLLM_BASE_URL = get("VLLM_BASE_URL", "http://192.168.6.241:8000/v1")
VLLM_MODEL = get("VLLM_MODEL", "nvidia/Qwen3-Next-80B-A3B-Instruct-NVFP4")
PORTFOLIO_DB_PATH = get("PORTFOLIO_DB_PATH", "/share/portfolio-dashboard/portfolio.db")
PRICE_UPDATE_INTERVAL = int(get("PRICE_UPDATE_INTERVAL", "15"))
DAILY_ANALYSIS_TIME = get("DAILY_ANALYSIS_TIME", "07:30")
WEEKLY_REVIEW_DAY = get("WEEKLY_REVIEW_DAY", "saturday")
WEEKLY_REVIEW_TIME = get("WEEKLY_REVIEW_TIME", "09:00")
SPARK_API_URL = get("SPARK_API_URL", "http://192.168.6.241:8080/api/v1")
SPARK_API_KEY = get("SPARK_API_KEY", "")
IBKR_API_URL = get("IBKR_API_URL", "http://127.0.0.1:8600")
IBKR_API_KEY = get("IBKR_API_KEY", "")
