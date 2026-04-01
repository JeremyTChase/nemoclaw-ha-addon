"""Portfolio calculations — values, weights, risk metrics."""

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

from . import db

logger = logging.getLogger("nemoclaw.portfolio")

_LSE_GBP = {"VJPN.L", "VWRP.L"}


def _get_gbpusd():
    macro = db.get_latest_macro()
    return macro.get("GBPUSD=X", {}).get("value", 1.30)


def price_to_gbp(ticker, raw_price):
    if ticker in _LSE_GBP:
        return raw_price
    elif ticker.endswith(".L"):
        return raw_price / 100.0
    else:
        return raw_price / _get_gbpusd()


def get_portfolio_summary(portfolio_id):
    """Returns list of position dicts with GBP values and weights."""
    positions = db.get_positions(portfolio_id)
    if not positions:
        return [], 0.0

    rows = []
    for p in positions:
        pr = db.get_latest_price(p["ticker"])
        raw = pr["close"] if pr else 0
        gbp = price_to_gbp(p["ticker"], raw)
        mv = p["shares"] * gbp
        rows.append({
            "ticker": p["ticker"],
            "shares": p["shares"],
            "price_gbp": gbp,
            "market_value": mv,
        })

    total = sum(r["market_value"] for r in rows)
    for r in rows:
        r["weight"] = r["market_value"] / total if total > 0 else 0
    rows.sort(key=lambda x: x["weight"], reverse=True)

    return rows, total


def format_portfolio_text(portfolio_id):
    """Format portfolio as a Telegram-friendly text message."""
    rows, total = get_portfolio_summary(portfolio_id)
    if not rows:
        return "No positions found."

    lines = [f"*{portfolio_id.upper()}* — £{total:,.0f}\n"]
    for r in rows:
        lines.append(f"  {r['ticker']:12s} {r['weight']:>5.1%}  £{r['market_value']:>10,.0f}")
    return "\n".join(lines)


def fetch_prices():
    """Fetch latest prices for all tickers + macro indicators."""
    tickers = db.get_all_tickers()
    macro_tickers = ["^VIX", "GC=F", "CL=F", "^TNX", "GBPUSD=X"]
    all_tickers = tickers + macro_tickers

    if not all_tickers:
        return

    end = datetime.utcnow()
    start = end - timedelta(days=730)

    data = yf.download(all_tickers, start=start.strftime("%Y-%m-%d"),
                       end=end.strftime("%Y-%m-%d"), timeout=60)

    if data.empty:
        logger.warning("No price data returned")
        return

    close = data["Close"].ffill().dropna(axis=1, how="all")

    # Insert stock prices
    records = []
    for ticker in tickers:
        if ticker in close.columns:
            for date, price in close[ticker].dropna().items():
                records.append((ticker, date.strftime("%Y-%m-%d"), float(price), "GBP"))
    if records:
        db.insert_prices(records)
        logger.info(f"Inserted {len(records)} price records")

    # Insert macro
    macro_records = []
    for ticker in macro_tickers:
        if ticker in close.columns:
            for date, val in close[ticker].dropna().items():
                macro_records.append((ticker, date.strftime("%Y-%m-%d"), float(val)))
    if macro_records:
        db.insert_macro(macro_records)
        logger.info(f"Inserted {len(macro_records)} macro records")


def calculate_risk_metrics(portfolio_id):
    """Calculate and store risk metrics using historical price data."""
    positions = db.get_positions(portfolio_id)
    if not positions:
        return None

    tickers = [p["ticker"] for p in positions]
    weights = []
    returns_list = []

    # Get price series and calculate returns
    for p in positions:
        series = db.get_latest_price(p["ticker"])  # just need to check it exists
        if not series:
            continue

    # Build returns dataframe from prices table
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT ticker, date, close FROM prices WHERE ticker IN ({}) ORDER BY date".format(
                ",".join("?" for _ in tickers)
            ), tickers
        ).fetchall()

    if not rows:
        return None

    df = pd.DataFrame([dict(r) for r in rows])
    pivot = df.pivot(index="date", columns="ticker", values="close").ffill().dropna()

    if len(pivot) < 30:
        return None

    # Log returns
    log_ret = np.log(pivot / pivot.shift(1)).dropna()

    # Portfolio weights (market value weighted)
    summary, total = get_portfolio_summary(portfolio_id)
    w = np.array([next((s["weight"] for s in summary if s["ticker"] == t), 0) for t in log_ret.columns])

    port_returns = (log_ret.values @ w)

    metrics = {
        "volatility_annual": float(np.std(port_returns) * np.sqrt(252)),
        "sharpe_ratio": float(np.mean(port_returns) / np.std(port_returns) * np.sqrt(252)) if np.std(port_returns) > 0 else 0,
        "sortino_ratio": float(np.mean(port_returns) / np.std(port_returns[port_returns < 0]) * np.sqrt(252)) if np.any(port_returns < 0) else 0,
        "max_drawdown": float(_max_drawdown(port_returns)),
        "cvar_95": float(_cvar_95(port_returns)),
    }

    db.insert_risk_metrics(portfolio_id, metrics)
    return metrics


def _max_drawdown(returns):
    cumulative = np.cumprod(1 + returns)
    running_max = np.maximum.accumulate(cumulative)
    drawdown = (running_max - cumulative) / running_max
    return np.max(drawdown)


def _cvar_95(returns):
    sorted_ret = np.sort(returns)
    cutoff = int(len(sorted_ret) * 0.05)
    if cutoff == 0:
        cutoff = 1
    return -np.mean(sorted_ret[:cutoff])


def is_market_hours():
    """Check if any major market is open (LSE or NYSE)."""
    now = datetime.utcnow()
    hour = now.hour
    weekday = now.weekday()
    if weekday >= 5:
        return False
    return 7 <= hour <= 21
