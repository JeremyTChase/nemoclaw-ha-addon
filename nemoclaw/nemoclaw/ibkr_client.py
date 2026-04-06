"""Thin HTTP client for the ibkr_gateway add-on shim.

Used by tools.py to expose IBKR paper-account read-only state
(positions, orders, P&L, reconciliation vs Freetrade) to the agent.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from nemoclaw import config, db


def _headers() -> dict[str, str]:
    return {"X-API-Key": config.IBKR_API_KEY} if config.IBKR_API_KEY else {}


def _get(path: str, timeout: float = 15) -> Any:
    url = f"{config.IBKR_API_URL.rstrip('/')}{path}"
    r = requests.get(url, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def health() -> dict:
    try:
        return _get("/ibkr/health", timeout=5)
    except Exception as e:
        return {"ok": False, "error": str(e)}


def positions() -> list[dict]:
    return _get("/ibkr/portfolio")


def orders() -> list[dict]:
    return _get("/ibkr/orders")


def pnl(account: Optional[str] = None) -> dict:
    suffix = f"?account={account}" if account else ""
    return _get(f"/ibkr/pnl{suffix}")


def reconciliation() -> dict:
    """Compare Freetrade book vs IBKR mirror.

    Returns: {
      ibkr_total_gbp, freetrade_total_gbp, matches, mismatches,
      missing_in_ibkr, missing_in_freetrade
    }
    """
    ibkr_pos = positions()

    # Build IBKR map: (symbol, currency) -> shares + market_value (native ccy)
    ibkr_map: dict[tuple[str, str], dict] = {}
    for p in ibkr_pos:
        key = (p["symbol"].upper(), p["currency"].upper())
        ibkr_map[key] = p

    # Pull Freetrade positions across all portfolios from shared SQLite
    ft_rows: list[dict] = []
    for p in db.get_portfolios():
        for pos in db.get_positions(p["id"]):
            ft_rows.append({
                "portfolio_id": p["id"],
                "ticker": pos["ticker"],
                "shares": pos["shares"],
            })

    matches: list[dict] = []
    mismatches: list[dict] = []
    missing_in_ibkr: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for r in ft_rows:
        t = r["ticker"].upper()
        if t.endswith(".L"):
            sym, ccy = t[:-2], "GBP"
        else:
            sym, ccy = t, "USD"
        key = (sym, ccy)
        seen.add(key)
        ft_shares = float(r["shares"])
        ibkr = ibkr_map.get(key)
        if ibkr is None or float(ibkr["position"]) == 0:
            missing_in_ibkr.append({
                "ticker": r["ticker"],
                "portfolio_id": r["portfolio_id"],
                "freetrade_shares": ft_shares,
            })
            continue
        delta = float(ibkr["position"]) - ft_shares
        rec = {
            "ticker": r["ticker"],
            "ibkr_symbol": sym,
            "currency": ccy,
            "freetrade_shares": ft_shares,
            "ibkr_shares": float(ibkr["position"]),
            "share_delta": delta,
            "ibkr_market_value": float(ibkr["market_value"]),
            "ibkr_unrealized_pnl": float(ibkr["unrealized_pnl"]),
        }
        if abs(delta) < 0.5:
            matches.append(rec)
        else:
            mismatches.append(rec)

    missing_in_freetrade = [
        {"symbol": k[0], "currency": k[1], "ibkr_shares": float(v["position"])}
        for k, v in ibkr_map.items() if k not in seen and float(v["position"]) != 0
    ]

    # Crude totals (in native currency, not FX-converted)
    totals_native: dict[str, float] = {}
    for p in ibkr_pos:
        totals_native[p["currency"]] = totals_native.get(p["currency"], 0.0) + float(p["market_value"])

    return {
        "ibkr_totals_native_ccy": totals_native,
        "n_matches": len(matches),
        "n_mismatches": len(mismatches),
        "n_missing_in_ibkr": len(missing_in_ibkr),
        "n_missing_in_freetrade": len(missing_in_freetrade),
        "matches": matches,
        "mismatches": mismatches,
        "missing_in_ibkr": missing_in_ibkr,
        "missing_in_freetrade": missing_in_freetrade,
    }
