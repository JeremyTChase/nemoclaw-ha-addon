"""Shared tool layer — pure return-value functions for portfolio operations.

This module is the single source of truth for portfolio tool logic. Both
the nemoclaw CLI (cli.py) and the Streamlit dashboard agent import from here.

Design rules:
- Every function returns a plain Python dict / list — no printing, no side
  effects beyond what the underlying module does (e.g. db writes for optimize).
- Errors are raised as exceptions (RuntimeError, ValueError) — callers decide
  how to surface them.
- Read-only vs write is marked in the READONLY_TOOLS / WRITE_TOOLS registries
  at the bottom of the file so the dashboard can restrict itself.
"""

from __future__ import annotations

from typing import Any, Optional

from nemoclaw import db, portfolio, tasks
from nemoclaw.ticker_search import search_tickers as _search_tickers

# ── ETF awareness (duplicated from cli.py so both import from here) ──
KNOWN_ETFS: set[str] = {
    "ISF.L", "IUSA.L", "VJPN.L", "VWRP.L", "VUSA.L", "VUKE.L",
    "VMID.L", "VFEM.L", "VWRL.L", "VEVE.L", "CSP1.L", "SWDA.L",
    "EQQQ.L", "RBOT.L", "INRG.L", "IEMG.L",
    "SPY", "QQQ", "VTI", "VOO", "IWM", "VEA", "VWO", "BND", "AGG",
    "GLD", "SLV", "ARKK", "XLF", "XLE", "XLK",
}
ETF_W_MIN = 0.05
STOCK_W_MIN = 0.02
STOCK_W_MAX = 0.25


def _portfolios_for(account: Optional[str]) -> list[dict]:
    ps = db.get_portfolios()
    if account:
        ps = [p for p in ps if p["id"] == account]
    return ps


# ── READ-ONLY TOOLS ───────────────────────────────────────────────────

def get_portfolio(account: Optional[str] = None) -> dict:
    """Return portfolio summary for one or all accounts.

    Returns: {accounts: [{id, name, total_gbp, positions: [{ticker, shares,
             weight, market_value, price}]}]}
    """
    out = []
    for p in _portfolios_for(account):
        rows, total = portfolio.get_portfolio_summary(p["id"])
        out.append({
            "id": p["id"],
            "name": p["name"],
            "total_gbp": total,
            "positions": [
                {
                    "ticker": r["ticker"],
                    "shares": r.get("shares"),
                    "weight": r["weight"],
                    "market_value": r["market_value"],
                    "price": r.get("price"),
                }
                for r in rows
            ],
        })
    return {"accounts": out}


def get_risk(account: Optional[str] = None) -> dict:
    out = []
    for p in _portfolios_for(account):
        m = db.get_latest_risk_metrics(p["id"])
        if not m:
            out.append({"id": p["id"], "name": p["name"], "metrics": None})
            continue
        out.append({
            "id": p["id"],
            "name": p["name"],
            "metrics": {
                "sharpe_ratio": m["sharpe_ratio"],
                "sortino_ratio": m["sortino_ratio"],
                "volatility_annual": m["volatility_annual"],
                "max_drawdown": m["max_drawdown"],
                "cvar_95": m["cvar_95"],
            },
        })
    return {"accounts": out}


def get_risk_history(account: Optional[str] = None, days: int = 30) -> dict:
    out = []
    for p in _portfolios_for(account):
        history = db.get_risk_history(p["id"], days=days) or []
        out.append({"id": p["id"], "name": p["name"], "history": history})
    return {"days": days, "accounts": out}


def get_value_history(account: Optional[str] = None, days: int = 30) -> dict:
    out = []
    for p in _portfolios_for(account):
        history = db.get_portfolio_total_value_history(p["id"], days=days) or []
        out.append({"id": p["id"], "name": p["name"], "history": history})
    return {"days": days, "accounts": out}


def get_trades(account: Optional[str] = None, limit: int = 20) -> dict:
    trades = db.get_transaction_log(portfolio_id=account, limit=limit) or []
    return {"account": account, "limit": limit, "trades": trades}


def get_macro() -> dict:
    macro = db.get_latest_macro() or {}
    labels = {
        "^VIX": "VIX (Fear)",
        "GC=F": "Gold ($/oz)",
        "CL=F": "Oil ($/bbl)",
        "^TNX": "US 10Y (%)",
        "GBPUSD=X": "GBP/USD",
    }
    indicators = [
        {
            "symbol": sym,
            "label": labels.get(sym, sym),
            "value": data["value"],
            "date": data["date"],
        }
        for sym, data in sorted(macro.items())
    ]
    vix = macro.get("^VIX", {}).get("value")
    context = None
    if vix is not None:
        if vix > 30:
            context = f"VIX at {vix:.1f} — high fear"
        elif vix > 20:
            context = f"VIX at {vix:.1f} — elevated caution"
        else:
            context = f"VIX at {vix:.1f} — calm markets"
    return {"indicators": indicators, "context": context}


def get_drift(account: Optional[str] = None) -> dict:
    out = []
    for p in _portfolios_for(account):
        opt = db.get_latest_optimization(p["id"])
        if not opt:
            out.append({"id": p["id"], "name": p["name"], "drift": None,
                        "message": "no optimizer targets — run optimize first"})
            continue
        rows, _ = portfolio.get_portfolio_summary(p["id"])
        current = {r["ticker"]: r["weight"] for r in rows}
        target = opt["weights"]
        entries = []
        total_drift = 0.0
        for t in sorted(set(list(current) + list(target))):
            cw = current.get(t, 0)
            tw = target.get(t, 0)
            d = cw - tw
            total_drift += abs(d)
            entries.append({"ticker": t, "current": cw, "target": tw, "drift": d})
        if total_drift > 0.20:
            verdict = "significant drift — consider rebalancing"
        elif total_drift > 0.10:
            verdict = "moderate drift — monitor"
        else:
            verdict = "within tolerance"
        out.append({
            "id": p["id"], "name": p["name"],
            "run_date": opt["run_date"],
            "total_drift": total_drift,
            "verdict": verdict,
            "drift": entries,
        })
    return {"accounts": out}


def get_news() -> dict:
    return {"news": tasks.fetch_relevant_news() or []}


def get_alerts() -> dict:
    return {"alerts": tasks.check_smart_alerts() or []}


def search_ticker(query: str) -> dict:
    return {"query": query, "results": _search_tickers(query) or []}


def get_last_optimize(account: Optional[str] = None) -> dict:
    out = []
    for p in _portfolios_for(account):
        opt = db.get_latest_optimization(p["id"])
        out.append({"id": p["id"], "name": p["name"], "result": opt})
    return {"accounts": out}


def look_through(account: Optional[str] = None) -> dict:
    from nemoclaw.etf_holdings import compute_look_through, get_overlap_warnings
    out = []
    for p in _portfolios_for(account):
        rows, total = portfolio.get_portfolio_summary(p["id"])
        if not rows:
            continue
        current = {r["ticker"]: r["weight"] for r in rows}
        exposure = compute_look_through(current)
        overlaps = get_overlap_warnings(current) or []
        out.append({
            "id": p["id"],
            "name": p["name"],
            "total_gbp": total,
            "overlaps": overlaps,
            "exposure": exposure,
        })
    return {"accounts": out}


# ── SPARK OPTIMIZER TOOLS (slow — call DGX Spark) ────────────────────

def _etf_overrides_for(tickers: list[str]) -> Optional[dict[str, float]]:
    etfs = [t for t in tickers if t in KNOWN_ETFS]
    return {t: ETF_W_MIN for t in etfs} if etfs else None


def optimize(account: Optional[str] = None, store: bool = True) -> dict:
    """Run NVIDIA CVaR optimizer for one or all accounts."""
    from nemoclaw import spark_client
    from nemoclaw.etf_holdings import get_all_etf_holdings

    out = []
    for p in _portfolios_for(account):
        positions = db.get_positions(p["id"])
        if not positions:
            out.append({"id": p["id"], "name": p["name"], "error": "no positions"})
            continue
        tickers = [pos["ticker"] for pos in positions]
        rows, total = portfolio.get_portfolio_summary(p["id"])
        current = {r["ticker"]: r["weight"] for r in rows}
        etf_overrides = _etf_overrides_for(tickers)
        etf_constituents = get_all_etf_holdings(tickers) or None

        result = spark_client.optimize(
            tickers=tickers,
            w_min=STOCK_W_MIN,
            w_max=STOCK_W_MAX,
            existing_weights=current,
            w_min_override=etf_overrides,
            etf_constituents=etf_constituents,
        )
        opt_weights = result["weights"]
        metrics = result["metrics"]

        # Build comparison rows
        changes = []
        for t in sorted(set(list(current) + list(opt_weights))):
            cw = current.get(t, 0)
            ow = opt_weights.get(t, 0)
            delta = ow - cw
            if abs(delta) < 0.005:
                action = "HOLD"
            elif delta > 0:
                action = "BUY"
            else:
                action = "SELL"
            changes.append({"ticker": t, "current": cw, "optimal": ow,
                            "delta": delta, "action": action})

        if store:
            db.insert_optimization_result(
                p["id"], "on_demand", opt_weights,
                result.get("cash", 0), metrics["expected_return"],
                metrics["cvar"], result["solver_used"],
            )

        out.append({
            "id": p["id"],
            "name": p["name"],
            "total_gbp": total,
            "solver": result["solver_used"],
            "num_scenarios": result["num_scenarios"],
            "metrics": metrics,
            "cash": result.get("cash", 0),
            "weights": opt_weights,
            "changes": changes,
            "etf_count": len(etf_overrides or {}),
            "look_through_applied": etf_constituents is not None,
        })
    return {"accounts": out}


def consider(ticker: str, account: str = "sip") -> dict:
    """Evaluate a specific ticker — what does the optimizer think?"""
    from nemoclaw import spark_client

    ticker = ticker.upper()
    positions = db.get_positions(account)
    if not positions:
        raise ValueError(f"no positions for {account}")

    existing = [pos["ticker"] for pos in positions]
    rows, total = portfolio.get_portfolio_summary(account)
    current = {r["ticker"]: r["weight"] for r in rows}

    is_new = ticker not in existing
    if is_new:
        all_tickers = existing + [ticker]
        current[ticker] = 0.0
    else:
        all_tickers = existing

    etf_overrides = _etf_overrides_for(all_tickers)
    result_with = spark_client.optimize(
        tickers=all_tickers,
        w_min=STOCK_W_MIN, w_max=STOCK_W_MAX,
        existing_weights=current,
        w_min_override=etf_overrides,
    )
    opt = result_with["weights"]
    recommended = opt.get(ticker, 0.0)
    metrics_with = result_with["metrics"]

    metrics_without = None
    if is_new:
        try:
            result_without = spark_client.optimize(
                tickers=existing,
                w_min=STOCK_W_MIN, w_max=STOCK_W_MAX,
                existing_weights={t: w for t, w in current.items() if t != ticker},
                w_min_override=_etf_overrides_for(existing),
            )
            metrics_without = result_without["metrics"]
        except RuntimeError:
            metrics_without = None

    # Verdict
    if is_new:
        if recommended > 0.10:
            verdict = "STRONG BUY"
        elif recommended > 0.05:
            verdict = "BUY"
        elif recommended > 0.02:
            verdict = "SMALL POSITION"
        else:
            verdict = "SKIP"
    else:
        cw = current[ticker]
        delta = recommended - cw
        if abs(delta) < 0.005:
            verdict = "HOLD"
        elif delta > 0.05:
            verdict = "INCREASE"
        elif delta > 0:
            verdict = "SLIGHT INCREASE"
        elif delta < -0.05:
            verdict = "REDUCE"
        else:
            verdict = "SLIGHT TRIM"

    # Shifts
    shifts = []
    for t in sorted(set(list(current) + list(opt))):
        if t == ticker:
            continue
        cw = current.get(t, 0)
        ow = opt.get(t, 0)
        d = ow - cw
        if abs(d) > 0.005:
            shifts.append({"ticker": t, "current": cw, "optimal": ow, "delta": d})
    shifts.sort(key=lambda x: abs(x["delta"]), reverse=True)

    return {
        "ticker": ticker,
        "account": account,
        "is_new": is_new,
        "is_etf": ticker in KNOWN_ETFS,
        "current_weight": current.get(ticker, 0.0),
        "recommended_weight": recommended,
        "verdict": verdict,
        "metrics_with": metrics_with,
        "metrics_without": metrics_without,
        "shifts": shifts[:10],
    }


def stress_test(scenario: str, account: Optional[str] = None) -> dict:
    from nemoclaw import spark_client

    if scenario not in spark_client.STRESS_SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}. "
                         f"Available: {spark_client.STRESS_SCENARIOS}")

    out = []
    for p in _portfolios_for(account):
        positions = db.get_positions(p["id"])
        if not positions:
            continue
        tickers = [pos["ticker"] for pos in positions]
        result = spark_client.stress_test(
            tickers=tickers, scenario_name=scenario,
            w_min=STOCK_W_MIN, w_max=STOCK_W_MAX,
        )
        stress = result["stress_case"]
        db.insert_optimization_result(
            p["id"], "stress_test", stress["weights"],
            stress.get("cash", 0), stress["metrics"]["expected_return"],
            stress["metrics"]["cvar"], stress.get("solver_used", ""),
            stress_scenario=scenario,
        )
        out.append({
            "id": p["id"],
            "name": p["name"],
            "scenario": scenario,
            "description": result["scenario_description"],
            "base_metrics": result["base_case"]["metrics"],
            "stress_metrics": stress["metrics"],
            "hedge_trades": result.get("hedge_trades", []),
            "delta_weights": result.get("delta_weights", {}),
        })
    return {"accounts": out}


def frontier(account: Optional[str] = None, num_points: int = 12) -> dict:
    from nemoclaw import spark_client
    out = []
    for p in _portfolios_for(account):
        positions = db.get_positions(p["id"])
        if not positions:
            continue
        tickers = [pos["ticker"] for pos in positions]
        result = spark_client.frontier(tickers=tickers, num_points=num_points)
        out.append({
            "id": p["id"],
            "name": p["name"],
            "points": result.get("points", []),
        })
    return {"accounts": out}


def backtest(account: str = "sip", use_optimal: bool = False) -> dict:
    from nemoclaw import spark_client
    positions = db.get_positions(account)
    if not positions:
        raise ValueError(f"no positions for {account}")
    tickers = [pos["ticker"] for pos in positions]

    if use_optimal:
        opt = db.get_latest_optimization(account)
        if not opt:
            raise RuntimeError("no optimization result — run optimize first")
        weights = opt["weights"]
        cash = opt.get("cash", 0)
        label = "Optimal"
    else:
        rows, _ = portfolio.get_portfolio_summary(account)
        weights = {r["ticker"]: r["weight"] for r in rows}
        cash = 0.0
        label = "Current"

    result = spark_client.backtest(tickers=tickers, weights=weights, cash=cash)
    return {
        "account": account,
        "label": label,
        "weights": weights,
        "cash": cash,
        "metrics": result,
    }


# ── WRITE TOOLS (NOT registered for dashboard — CLI/Telegram only) ───

def buy(account: str, ticker: str, shares: float) -> dict:
    ticker = ticker.upper()
    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)
    new_total = current + shares
    db.upsert_position(account, ticker, new_total)
    action = "increased" if current > 0 else "added"
    db.log_transaction(account, ticker, action, current, new_total)
    portfolio.fetch_prices()
    return {"account": account, "ticker": ticker, "action": action,
            "shares_before": current, "shares_after": new_total, "delta": shares}


def sell(account: str, ticker: str, shares) -> dict:
    ticker = ticker.upper()
    positions = db.get_positions(account)
    current = next((p["shares"] for p in positions if p["ticker"] == ticker), 0)
    if current <= 0:
        raise ValueError(f"no {ticker} position in {account}")
    if isinstance(shares, str) and shares.lower() == "all":
        sell_qty = current
    else:
        sell_qty = float(shares)
    new_total = current - sell_qty
    if new_total < 0.001:
        db.delete_position(account, ticker)
        db.log_transaction(account, ticker, "removed", current, 0)
        new_total = 0
        action = "removed"
    else:
        db.upsert_position(account, ticker, new_total)
        db.log_transaction(account, ticker, "decreased", current, new_total)
        action = "decreased"
    return {"account": account, "ticker": ticker, "action": action,
            "shares_before": current, "shares_after": new_total, "delta": -sell_qty}


# ── Tool registries ───────────────────────────────────────────────────
# The dashboard agent registers READONLY_TOOLS only.
# The CLI registers both READONLY_TOOLS and WRITE_TOOLS.

READONLY_TOOLS: dict[str, Any] = {
    "get_portfolio": get_portfolio,
    "get_risk": get_risk,
    "get_risk_history": get_risk_history,
    "get_value_history": get_value_history,
    "get_trades": get_trades,
    "get_macro": get_macro,
    "get_drift": get_drift,
    "get_news": get_news,
    "get_alerts": get_alerts,
    "search_ticker": search_ticker,
    "get_last_optimize": get_last_optimize,
    "look_through": look_through,
    "optimize": optimize,
    "consider": consider,
    "stress_test": stress_test,
    "frontier": frontier,
    "backtest": backtest,
}

WRITE_TOOLS: dict[str, Any] = {
    "buy": buy,
    "sell": sell,
}
