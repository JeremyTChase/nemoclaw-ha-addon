"""SQLite access layer — reads/writes the shared portfolio.db."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime

from . import config


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.PORTFOLIO_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_portfolios():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM portfolios").fetchall()]


def get_positions(portfolio_id):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE portfolio_id=?", (portfolio_id,)
        ).fetchall()]


def get_all_tickers():
    with get_conn() as conn:
        rows = conn.execute("SELECT DISTINCT ticker FROM positions").fetchall()
        return [r["ticker"] for r in rows]


def upsert_position(portfolio_id, ticker, shares, avg_cost=None, currency="GBP"):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO positions (portfolio_id, ticker, shares, avg_cost_basis, currency, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, ticker) DO UPDATE SET "
            "shares=excluded.shares, avg_cost_basis=COALESCE(excluded.avg_cost_basis, avg_cost_basis), "
            "currency=excluded.currency, last_updated=excluded.last_updated",
            (portfolio_id, ticker, shares, avg_cost, currency, now),
        )


def delete_position(portfolio_id, ticker):
    with get_conn() as conn:
        conn.execute("DELETE FROM positions WHERE portfolio_id=? AND ticker=?",
                     (portfolio_id, ticker))


def get_latest_price(ticker):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT close, date FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        return dict(row) if row else None


def insert_prices(records):
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO prices (ticker, date, close, currency) VALUES (?, ?, ?, ?)",
            records,
        )


def get_latest_macro():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT indicator, value, date FROM macro_indicators "
            "WHERE (indicator, date) IN "
            "(SELECT indicator, MAX(date) FROM macro_indicators GROUP BY indicator)"
        ).fetchall()
        return {r["indicator"]: {"value": r["value"], "date": r["date"]} for r in rows}


def insert_macro(records):
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO macro_indicators (indicator, date, value) VALUES (?, ?, ?)",
            records,
        )


def insert_agent_log(task_type, summary, full_analysis="", severity="info"):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO agent_logs (task_type, created_at, summary, full_analysis, severity) "
            "VALUES (?, ?, ?, ?, ?)",
            (task_type, now, summary, full_analysis, severity),
        )


def get_latest_risk_metrics(portfolio_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM risk_metrics WHERE portfolio_id=? ORDER BY calculated_at DESC LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return dict(row) if row else None


def insert_risk_metrics(portfolio_id, metrics):
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO risk_metrics (portfolio_id, calculated_at, volatility_annual, "
            "sharpe_ratio, sortino_ratio, max_drawdown, cvar_95) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (portfolio_id, now, metrics.get("volatility_annual"),
             metrics.get("sharpe_ratio"), metrics.get("sortino_ratio"),
             metrics.get("max_drawdown"), metrics.get("cvar_95")),
        )


def log_transaction(portfolio_id, ticker, action, shares_before, shares_after):
    now = datetime.utcnow().isoformat()
    delta = shares_after - shares_before
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO transaction_log "
            "(portfolio_id, logged_at, ticker, action, shares_before, shares_after, shares_delta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (portfolio_id, now, ticker, action, shares_before, shares_after, delta),
        )


def take_position_snapshot(portfolio_id, snapshot_date=None):
    if snapshot_date is None:
        snapshot_date = datetime.utcnow().strftime("%Y-%m-%d")
    positions = get_positions(portfolio_id)
    if not positions:
        return None

    # GBP conversion
    _LSE_GBP = {"VJPN.L", "VWRP.L"}
    macro = get_latest_macro()
    gbpusd = macro.get("GBPUSD=X", {}).get("value", 1.30)

    total = 0.0
    pos_data = []
    for p in positions:
        pr = get_latest_price(p["ticker"])
        raw_price = pr["close"] if pr else 0
        ticker = p["ticker"]
        if ticker in _LSE_GBP:
            gbp = raw_price
        elif ticker.endswith(".L"):
            gbp = raw_price / 100
        else:
            gbp = raw_price / gbpusd
        mv = p["shares"] * gbp
        total += mv
        pos_data.append((ticker, p["shares"], gbp, mv))

    with get_conn() as conn:
        for ticker, shares, price, mv in pos_data:
            weight = mv / total if total > 0 else 0
            conn.execute(
                "INSERT OR REPLACE INTO position_snapshots "
                "(portfolio_id, snapshot_date, ticker, shares, price, market_value, weight) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (portfolio_id, snapshot_date, ticker, shares, price, mv, weight),
            )

    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO risk_metrics_history "
            "(portfolio_id, date, total_value, volatility_annual, sharpe_ratio, "
            "sortino_ratio, max_drawdown, cvar_95) "
            "SELECT ?, ?, ?, volatility_annual, sharpe_ratio, sortino_ratio, max_drawdown, cvar_95 "
            "FROM risk_metrics WHERE portfolio_id=? ORDER BY calculated_at DESC LIMIT 1",
            (portfolio_id, snapshot_date, total, portfolio_id),
        )

    return total
