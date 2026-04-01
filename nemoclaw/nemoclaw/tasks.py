"""Scheduled tasks — price updates, alerts, analysis."""

import logging
from datetime import datetime

from . import db, llm, portfolio

logger = logging.getLogger("nemoclaw.tasks")


def run_price_update():
    if portfolio.is_market_hours():
        portfolio.fetch_prices()


def check_price_alerts(threshold_daily=0.03, threshold_weekly=0.05):
    """Check for significant price moves. Returns list of alert strings."""
    alerts = []
    tickers = db.get_all_tickers()

    with db.get_conn() as conn:
        for ticker in tickers:
            rows = conn.execute(
                "SELECT close FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 6",
                (ticker,),
            ).fetchall()
            if len(rows) < 2:
                continue

            today_price = rows[0]["close"]
            yesterday_price = rows[1]["close"]
            daily_change = (today_price - yesterday_price) / yesterday_price

            if abs(daily_change) >= threshold_daily:
                direction = "up" if daily_change > 0 else "down"
                alerts.append(f"{ticker} moved {daily_change:+.1%} today ({direction})")

            if len(rows) >= 6:
                week_ago_price = rows[5]["close"]
                weekly_change = (today_price - week_ago_price) / week_ago_price
                if abs(weekly_change) >= threshold_weekly:
                    direction = "up" if weekly_change > 0 else "down"
                    alerts.append(f"{ticker} moved {weekly_change:+.1%} this week ({direction})")

    return alerts


def run_price_alerts_with_analysis():
    """Check alerts and get LLM analysis if any found."""
    if not portfolio.is_market_hours():
        return None

    alerts = check_price_alerts()
    if not alerts:
        return None

    # Get portfolio context
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))

    alert_text = "\n".join(alerts)
    prompt = (
        f"Portfolio:\n{''.join(context_parts)}\n\n"
        f"Price alerts:\n{alert_text}\n\n"
        "Briefly explain these moves and what they mean for this portfolio."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=800)
    db.insert_agent_log("price_alert", alert_text[:200], analysis, "warning")
    return f"Price Alerts:\n{alert_text}\n\n{analysis}"


def run_daily_analysis():
    """Morning market brief."""
    context_parts = []
    for p in db.get_portfolios():
        context_parts.append(portfolio.format_portfolio_text(p["id"]))
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(
                f"Risk: Sharpe={m['sharpe_ratio']:.2f}, "
                f"Vol={m['volatility_annual']:.1%}, MDD={m['max_drawdown']:.1%}"
            )

    macro = db.get_latest_macro()
    macro_lines = [f"  {k}: {v['value']:.2f}" for k, v in macro.items()]
    context_parts.append(f"Macro indicators:\n" + "\n".join(macro_lines))

    prompt = (
        "\n\n".join(context_parts) + "\n\n"
        "Provide a concise morning brief: key overnight moves, what to watch today, "
        "and any concerns for this portfolio. Consider macro/geopolitical factors."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=1500)
    db.insert_agent_log("daily_analysis", analysis[:200], analysis)
    logger.info("Daily analysis complete")
    return analysis


def run_weekly_review():
    """Weekend portfolio review with rebalancing suggestions."""
    context_parts = []
    for p in db.get_portfolios():
        rows, total = portfolio.get_portfolio_summary(p["id"])
        context_parts.append(f"{p['name']} (£{total:,.0f}):")
        for r in rows:
            context_parts.append(f"  {r['ticker']:12s} {r['weight']:>5.1%}  £{r['market_value']:>10,.0f}")
        m = db.get_latest_risk_metrics(p["id"])
        if m:
            context_parts.append(
                f"  Risk: Sharpe={m['sharpe_ratio']:.2f}, Vol={m['volatility_annual']:.1%}, "
                f"MDD={m['max_drawdown']:.1%}, CVaR={m['cvar_95']:.2%}"
            )

    prompt = (
        "\n".join(context_parts) + "\n\n"
        "Weekly review: assess concentration risk, sector balance, "
        "geographic exposure (UK vs US), and suggest any rebalancing. "
        "Consider current macro environment. Be specific about which "
        "positions to adjust and why."
    )

    analysis = llm.chat([{"role": "user", "content": prompt}], max_tokens=2000)
    db.insert_agent_log("weekly_review", analysis[:200], analysis)
    logger.info("Weekly review complete")
    return analysis


def run_daily_snapshot():
    """Take daily snapshot of all portfolios."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for p in db.get_portfolios():
        portfolio.calculate_risk_metrics(p["id"])
        db.take_position_snapshot(p["id"], today)
    logger.info(f"Daily snapshot taken for {today}")
