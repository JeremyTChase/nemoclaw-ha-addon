"""Telegram bot — NemoClaw's interface to Jeremy."""

import logging
import re

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes
)

from . import config, db, llm, portfolio
from .ticker_search import search_tickers

logger = logging.getLogger("nemoclaw.bot")


def _auth(func):
    """Decorator: only allow messages from Jeremy's user ID and chat ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        if user_id != config.TELEGRAM_USER_ID:
            logger.warning(f"Unauthorized user: {user_id}")
            await update.message.reply_text("Unauthorized.")
            return
        if config.TELEGRAM_CHAT_ID and chat_id != config.TELEGRAM_CHAT_ID:
            logger.warning(f"Unauthorized chat: {chat_id}")
            await update.message.reply_text("Unauthorized chat.")
            return
        return await func(update, context)
    return wrapper


# --- Commands ---

@_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "NemoClaw online.\n\n"
        "Commands:\n"
        "/portfolio — view positions & values\n"
        "/sip — SIP (SIPP) summary\n"
        "/isa — SS ISA summary\n"
        "/risk — risk metrics\n"
        "/buy TICKER SHARES — log a buy\n"
        "/sell TICKER SHARES — log a sell\n"
        "/search QUERY — find a ticker\n"
        "/analyse — run market analysis now\n"
        "/alerts — check price alerts now\n"
        "/help — show this message"
    )


@_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)


@_auth
async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texts = []
    for p in db.get_portfolios():
        texts.append(portfolio.format_portfolio_text(p["id"]))
    await update.message.reply_text("\n\n".join(texts), parse_mode="Markdown")


@_auth
async def cmd_sip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = portfolio.format_portfolio_text("sip")
    await update.message.reply_text(text, parse_mode="Markdown")


@_auth
async def cmd_isa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = portfolio.format_portfolio_text("ss_isa")
    await update.message.reply_text(text, parse_mode="Markdown")


@_auth
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    for p in db.get_portfolios():
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            lines.append(
                f"*{p['name']}*\n"
                f"  Sharpe: {m['sharpe_ratio']:.2f}\n"
                f"  Sortino: {m['sortino_ratio']:.2f}\n"
                f"  Vol: {m['volatility_annual']:.1%}\n"
                f"  Max DD: {m['max_drawdown']:.1%}\n"
                f"  CVaR 95%: {m['cvar_95']:.2%}"
            )
        else:
            lines.append(f"*{p['name']}*: no risk data yet")
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


@_auth
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /buy TICKER SHARES [ACCOUNT]"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /buy TICKER SHARES [sip|isa]")
        return

    ticker = args[0].upper()
    try:
        shares = float(args[1])
    except ValueError:
        await update.message.reply_text("Invalid share count.")
        return

    account = "sip"
    if len(args) >= 3:
        account = "ss_isa" if "isa" in args[2].lower() else "sip"

    # Get current position
    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)
    new_total = current + shares

    db.upsert_position(account, ticker, new_total)

    action = "increased" if current > 0 else "added"
    db.log_transaction(account, ticker, action, current, new_total)

    await update.message.reply_text(
        f"Bought {shares:.2f} {ticker} in {account.upper()}\n"
        f"Position: {current:.2f} -> {new_total:.2f}"
    )

    # Fetch price for new ticker
    portfolio.fetch_prices()


@_auth
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /sell TICKER SHARES [ACCOUNT] or /sell TICKER all [ACCOUNT]"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /sell TICKER SHARES [sip|isa]\nUse 'all' to sell entire position.")
        return

    ticker = args[0].upper()

    account = "sip"
    if len(args) >= 3:
        account = "ss_isa" if "isa" in args[2].lower() else "sip"

    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)

    if current <= 0:
        await update.message.reply_text(f"No {ticker} position in {account.upper()}")
        return

    if args[1].lower() == "all":
        shares = current
    else:
        try:
            shares = float(args[1])
        except ValueError:
            await update.message.reply_text("Invalid share count.")
            return

    new_total = current - shares
    if new_total < 0.001:
        db.delete_position(account, ticker)
        db.log_transaction(account, ticker, "removed", current, 0)
        await update.message.reply_text(f"Sold all {ticker} in {account.upper()} — position closed")
    else:
        db.upsert_position(account, ticker, new_total)
        db.log_transaction(account, ticker, "decreased", current, new_total)
        await update.message.reply_text(
            f"Sold {shares:.2f} {ticker} in {account.upper()}\n"
            f"Position: {current:.2f} -> {new_total:.2f}"
        )


@_auth
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /search QUERY"""
    if not context.args:
        await update.message.reply_text("Usage: /search company name or ticker")
        return

    query = " ".join(context.args)
    results = search_tickers(query)

    if not results:
        await update.message.reply_text(f"No results for '{query}'")
        return

    lines = [f"Results for '{query}':"]
    for r in results[:8]:
        lines.append(f"  {r['symbol']:12s} — {r['name']} ({r['exchange']})")
    await update.message.reply_text("\n".join(lines))


@_auth
async def cmd_analyse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run market analysis on demand."""
    await update.message.reply_text("Running analysis...")

    # Build context
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(f"Risk: Sharpe={m['sharpe_ratio']:.2f}, Vol={m['volatility_annual']:.1%}")

    macro = db.get_latest_macro()
    macro_text = "\n".join(f"  {k}: {v['value']:.2f}" for k, v in macro.items())
    context_parts.append(f"Macro:\n{macro_text}")

    prompt = "\n\n".join(context_parts) + "\n\nProvide a concise portfolio analysis and any action items."
    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=1500)

    db.insert_agent_log("on_demand_analysis", analysis[:200], analysis)
    await update.message.reply_text(analysis)


@_auth
async def cmd_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check price alerts now."""
    from .tasks import check_price_alerts
    alerts = check_price_alerts()
    if alerts:
        await update.message.reply_text("\n".join(alerts))
    else:
        await update.message.reply_text("No significant price moves right now.")


@_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages — pass to LLM for natural conversation."""
    text = update.message.text

    # Quick trade parsing: "bought 50 AVGO" or "sold all MU"
    trade = _parse_trade_message(text)
    if trade:
        # Execute the trade
        positions = db.get_positions(trade["account"])
        current = next((p["shares"] for p in positions if p["ticker"] == trade["ticker"]), 0)

        if trade["action"] == "BUY":
            new_total = current + trade["shares"]
            db.upsert_position(trade["account"], trade["ticker"], new_total)
            action = "increased" if current > 0 else "added"
            db.log_transaction(trade["account"], trade["ticker"], action, current, new_total)
            await update.message.reply_text(
                f"Logged: bought {trade['shares']:.2f} {trade['ticker']} in {trade['account'].upper()}\n"
                f"Position: {current:.2f} -> {new_total:.2f}"
            )
        else:
            if trade["sell_all"] or trade["shares"] >= current:
                db.delete_position(trade["account"], trade["ticker"])
                db.log_transaction(trade["account"], trade["ticker"], "removed", current, 0)
                await update.message.reply_text(
                    f"Logged: sold all {trade['ticker']} in {trade['account'].upper()} — position closed"
                )
            else:
                new_total = current - trade["shares"]
                db.upsert_position(trade["account"], trade["ticker"], new_total)
                db.log_transaction(trade["account"], trade["ticker"], "decreased", current, new_total)
                await update.message.reply_text(
                    f"Logged: sold {trade['shares']:.2f} {trade['ticker']} in {trade['account'].upper()}\n"
                    f"Position: {current:.2f} -> {new_total:.2f}"
                )
        portfolio.fetch_prices()
        return

    # Otherwise, chat with LLM using portfolio context
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))

    system_context = "\n\n".join(context_parts)
    messages = [
        {"role": "user", "content": f"Portfolio context:\n{system_context}\n\nUser message: {text}"}
    ]

    response = llm.chat(messages, max_tokens=1000)
    await update.message.reply_text(response)


def _parse_trade_message(text):
    """Parse natural language trade messages.

    Matches patterns like:
    - "bought 50 AVGO"
    - "sold all MU"
    - "sold 100 RR.L in isa"
    - "bought 200 BARC.L sip"
    """
    text_lower = text.lower().strip()

    # Match: bought/sold [NUMBER|all] TICKER [in] [sip|isa]
    buy_match = re.match(
        r'(?:bought|buy|added)\s+(\d+\.?\d*)\s+([A-Za-z0-9.]+)(?:\s+(?:in\s+)?(\w+))?',
        text_lower
    )
    sell_match = re.match(
        r'(?:sold|sell|removed)\s+(all|\d+\.?\d*)\s+([A-Za-z0-9.]+)(?:\s+(?:in\s+)?(\w+))?',
        text_lower
    )

    if buy_match:
        shares = float(buy_match.group(1))
        ticker = buy_match.group(2).upper()
        account = _parse_account(buy_match.group(3))
        return {"action": "BUY", "ticker": ticker, "shares": shares, "sell_all": False, "account": account}

    if sell_match:
        sell_all = sell_match.group(1) == "all"
        shares = 0 if sell_all else float(sell_match.group(1))
        ticker = sell_match.group(2).upper()
        account = _parse_account(sell_match.group(3))
        return {"action": "SELL", "ticker": ticker, "shares": shares, "sell_all": sell_all, "account": account}

    return None


def _parse_account(text):
    """Parse account name from free text."""
    if text and ("isa" in text.lower()):
        return "ss_isa"
    return "sip"  # default to SIP


def build_app():
    """Build and return the Telegram bot application."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("sip", cmd_sip))
    app.add_handler(CommandHandler("isa", cmd_isa))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("analyse", cmd_analyse))
    app.add_handler(CommandHandler("analyze", cmd_analyse))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    return app
