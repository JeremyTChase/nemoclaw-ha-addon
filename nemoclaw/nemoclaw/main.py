"""NemoClaw main entry point — runs Telegram bot + scheduled tasks."""

import asyncio
import logging
import threading
import time

import schedule

from . import config, db, portfolio
from .bot import build_app
from .tasks import (
    run_price_update, run_price_alerts_with_analysis,
    run_daily_analysis, run_weekly_review, run_daily_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("nemoclaw")

# Reference to the bot app for sending proactive messages
_bot_app = None


async def _send_telegram(text):
    """Send a proactive message to Jeremy via Telegram."""
    if _bot_app and config.TELEGRAM_CHAT_ID:
        try:
            await _bot_app.bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=text[:4096],  # Telegram message limit
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
        analysis = run_daily_analysis()
        if analysis:
            _send_sync(f"Good morning.\n\n{analysis}")
    except Exception as e:
        logger.error(f"Daily analysis failed: {e}")


def _scheduled_price_alerts():
    try:
        result = run_price_alerts_with_analysis()
        if result:
            _send_sync(result)
    except Exception as e:
        logger.error(f"Price alerts failed: {e}")


def _scheduled_weekly_review():
    try:
        review = run_weekly_review()
        if review:
            _send_sync(f"Weekly Portfolio Review\n\n{review}")
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

    schedule.every().day.at(daily_time).do(_scheduled_daily_analysis)
    schedule.every().day.at("21:05").do(_scheduled_snapshot)
    schedule.every(interval).minutes.do(_scheduled_price_update)
    schedule.every(interval).minutes.do(_scheduled_price_alerts)
    getattr(schedule.every(), review_day).at(review_time).do(_scheduled_weekly_review)

    logger.info("Scheduler started:")
    logger.info(f"  Daily analysis: {daily_time} UTC")
    logger.info(f"  Daily snapshot: 21:05 UTC")
    logger.info(f"  Price updates: every {interval}min (market hours)")
    logger.info(f"  Price alerts: every {interval}min (market hours)")
    logger.info(f"  Weekly review: {review_day} {review_time} UTC")

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

    # Build and run Telegram bot (blocking — runs the event loop)
    _bot_app = build_app()
    logger.info("Telegram bot starting...")
    _bot_app.run_polling(allowed_updates=["message", "my_chat_member"])


if __name__ == "__main__":
    main()
