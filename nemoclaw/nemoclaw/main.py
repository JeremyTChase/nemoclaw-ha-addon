"""NemoClaw main entry point — runs Telegram bot + scheduled tasks."""

import asyncio
import logging
import threading
import time

import schedule

from . import config, db, portfolio
from .bot import build_app
from .tasks import (
    run_price_update, run_smart_alerts, run_news_check,
    run_daily_analysis, run_weekly_review, run_daily_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("nemoclaw")

_bot_app = None


async def _send_telegram(text):
    """Send a proactive message to Jeremy via Telegram."""
    if _bot_app and config.TELEGRAM_CHAT_ID:
        try:
            # Split long messages (Telegram limit is 4096 chars)
            for i in range(0, len(text), 4096):
                await _bot_app.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=text[i:i+4096],
                )
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")


def _send_sync(text):
    """Synchronous wrapper for sending Telegram messages from scheduler thread."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_send_telegram(text))
        else:
            loop.run_until_complete(_send_telegram(text))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_send_telegram(text))


def _scheduled_daily_analysis():
    try:
        msg = run_daily_analysis()
        if msg:
            _send_sync(msg)
    except Exception as e:
        logger.error(f"Daily analysis failed: {e}")


def _scheduled_smart_alerts():
    try:
        msg = run_smart_alerts()
        if msg:
            _send_sync(msg)
    except Exception as e:
        logger.error(f"Smart alerts failed: {e}")


def _scheduled_news_check():
    try:
        msg = run_news_check()
        if msg:
            _send_sync(msg)
    except Exception as e:
        logger.error(f"News check failed: {e}")


def _scheduled_weekly_review():
    try:
        msg = run_weekly_review()
        if msg:
            _send_sync(msg)
    except Exception as e:
        logger.error(f"Weekly review failed: {e}")


def _scheduled_price_update():
    try:
        run_price_update()
    except Exception as e:
        logger.error(f"Price update failed: {e}")


def _scheduled_snapshot():
    try:
        run_daily_snapshot()
    except Exception as e:
        logger.error(f"Daily snapshot failed: {e}")


def _scheduler_thread():
    """Run the schedule loop in a background thread."""
    daily_time = config.DAILY_ANALYSIS_TIME
    interval = config.PRICE_UPDATE_INTERVAL
    review_day = config.WEEKLY_REVIEW_DAY
    review_time = config.WEEKLY_REVIEW_TIME

    # Morning brief — once per day
    schedule.every().day.at(daily_time).do(_scheduled_daily_analysis)

    # Price updates — every 15min during market hours (silent)
    schedule.every(interval).minutes.do(_scheduled_price_update)

    # Smart alerts — every 30min during market hours (only fires on big moves, deduped)
    schedule.every(30).minutes.do(_scheduled_smart_alerts)

    # News check — every 2 hours during market hours
    schedule.every(2).hours.do(_scheduled_news_check)

    # Weekly review — weekend
    getattr(schedule.every(), review_day).at(review_time).do(_scheduled_weekly_review)

    # Daily snapshot — after market close (silent)
    schedule.every().day.at("21:05").do(_scheduled_snapshot)

    logger.info("Scheduler started:")
    logger.info(f"  Morning brief: {daily_time} UTC (once)")
    logger.info(f"  Price updates: every {interval}min (silent)")
    logger.info(f"  Smart alerts: every 30min (only significant moves, deduped)")
    logger.info(f"  News check: every 2 hours")
    logger.info(f"  Weekly review: {review_day} {review_time} UTC")
    logger.info(f"  Daily snapshot: 21:05 UTC (silent)")

    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    global _bot_app

    logger.info("NemoClaw starting...")
    logger.info(f"Telegram bot: @TheWolfAdvisor_bot")
    logger.info(f"Chat ID: {config.TELEGRAM_CHAT_ID}")
    logger.info(f"vLLM: {config.VLLM_BASE_URL}")
    logger.info(f"DB: {config.PORTFOLIO_DB_PATH}")

    # Initial price fetch
    logger.info("Running initial price fetch...")
    try:
        portfolio.fetch_prices()
    except Exception as e:
        logger.warning(f"Initial price fetch failed: {e}")

    # Start scheduler in background thread
    scheduler = threading.Thread(target=_scheduler_thread, daemon=True)
    scheduler.start()

    # Build and run Telegram bot (blocking)
    _bot_app = build_app()
    logger.info("Telegram bot starting...")
    _bot_app.run_polling(allowed_updates=["message", "my_chat_member"])


if __name__ == "__main__":
    main()
