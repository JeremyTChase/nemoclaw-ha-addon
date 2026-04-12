"""ETF constituent holdings for look-through analysis.

Provides top holdings + weights for ETFs in Jeremy's portfolio.
Used by:
  - look-through command: show true per-stock exposure
  - optimizer: pass to Spark for constituent-aware scenario generation

Holdings data refreshes are infrequent (ETFs rebalance quarterly).
Hardcoded top 20 as of Q1 2026 — update via refresh_holdings() or manually.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("jezclaw.etf")

# Cache file for refreshed holdings
_CACHE_DIR = os.environ.get("PORTFOLIO_DB_PATH", "").rsplit("/", 1)[0] or "/tmp"
_CACHE_FILE = Path(_CACHE_DIR) / "etf_holdings_cache.json"

# ── Hardcoded top holdings (Q1 2026) ───────────────────────────────
# Source: iShares/Vanguard factsheets
# Weights are approximate — top 20 constituents covering ~60-80% of fund

_BUILTIN_HOLDINGS: dict[str, dict[str, float]] = {
    # iShares Core FTSE 100 (ISF.L) — tracks FTSE 100
    "ISF.L": {
        "SHEL.L": 0.089, "AZN.L": 0.078, "HSBA.L": 0.058, "ULVR.L": 0.047,
        "BP.L": 0.037, "GSK.L": 0.035, "RIO.L": 0.033, "BARC.L": 0.030,
        "LSEG.L": 0.029, "DGE.L": 0.027, "REL.L": 0.025, "BAE.L": 0.024,
        "BATS.L": 0.023, "NG.L": 0.021, "AAL.L": 0.020, "IMB.L": 0.018,
        "NWG.L": 0.017, "ABF.L": 0.015, "CRH.L": 0.014, "RR.L": 0.013,
    },
    # iShares Core S&P 500 (IUSA.L) — tracks S&P 500
    "IUSA.L": {
        "AAPL": 0.072, "MSFT": 0.065, "NVDA": 0.062, "AMZN": 0.040,
        "META": 0.028, "GOOGL": 0.023, "GOOG": 0.019, "BRK-B": 0.018,
        "AVGO": 0.017, "TSLA": 0.016, "JPM": 0.015, "LLY": 0.014,
        "V": 0.012, "UNH": 0.012, "MA": 0.011, "XOM": 0.010,
        "COST": 0.009, "HD": 0.009, "PG": 0.009, "JNJ": 0.008,
    },
    # Vanguard FTSE Japan (VJPN.L) — tracks FTSE Japan
    "VJPN.L": {
        "7203.T": 0.053, "6758.T": 0.035, "8306.T": 0.030, "6501.T": 0.025,
        "8035.T": 0.024, "6902.T": 0.021, "9984.T": 0.020, "8316.T": 0.019,
        "7974.T": 0.018, "4063.T": 0.017, "9432.T": 0.016, "6861.T": 0.015,
        "6367.T": 0.014, "4502.T": 0.013, "6723.T": 0.012, "8411.T": 0.011,
        "7741.T": 0.010, "6098.T": 0.010, "3382.T": 0.009, "2914.T": 0.009,
    },
    # Vanguard FTSE All-World (VWRP.L) — tracks FTSE All-World (global)
    "VWRP.L": {
        "AAPL": 0.045, "MSFT": 0.041, "NVDA": 0.039, "AMZN": 0.025,
        "META": 0.017, "GOOGL": 0.014, "GOOG": 0.012, "TSLA": 0.010,
        "AVGO": 0.011, "BRK-B": 0.011, "JPM": 0.009, "LLY": 0.008,
        "7203.T": 0.006, "ASML": 0.006, "V": 0.007, "UNH": 0.007,
        "MA": 0.006, "XOM": 0.006, "SHEL.L": 0.005, "NESN.SW": 0.005,
    },
}


def get_holdings(etf_ticker: str) -> dict[str, float] | None:
    """Get top holdings for an ETF. Returns {constituent: weight} or None."""
    # Try cache first
    cached = _load_cache()
    if cached and etf_ticker in cached:
        return cached[etf_ticker]["holdings"]

    # Fall back to built-in
    return _BUILTIN_HOLDINGS.get(etf_ticker)


def get_all_etf_holdings(tickers: list[str]) -> dict[str, dict[str, float]]:
    """Get holdings for all ETFs in a ticker list.

    Returns {etf_ticker: {constituent: weight}} for recognised ETFs only.
    """
    result = {}
    for t in tickers:
        h = get_holdings(t)
        if h:
            result[t] = h
    return result


def compute_look_through(
    portfolio_weights: dict[str, float],
) -> dict[str, dict]:
    """Decompose portfolio into true per-stock exposure.

    Returns {stock: {"direct": weight, "via_etf": {etf: contribution}, "total": weight}}
    """
    exposure: dict[str, dict] = {}

    for ticker, weight in portfolio_weights.items():
        holdings = get_holdings(ticker)
        if holdings:
            # This is an ETF — decompose into constituents
            covered_weight = sum(holdings.values())
            residual = weight * (1.0 - covered_weight)  # unaccounted portion

            for constituent, const_weight in holdings.items():
                effective = weight * const_weight
                if constituent not in exposure:
                    exposure[constituent] = {"direct": 0.0, "via_etf": {}, "total": 0.0}
                exposure[constituent]["via_etf"][ticker] = effective
                exposure[constituent]["total"] += effective

            # Track the ETF itself (residual = smaller/unlisted constituents)
            if ticker not in exposure:
                exposure[ticker] = {"direct": 0.0, "via_etf": {}, "total": 0.0}
            exposure[ticker]["direct"] = weight
            exposure[ticker]["total"] = weight  # ETF as a whole
            exposure[ticker]["residual"] = residual
        else:
            # Direct holding
            if ticker not in exposure:
                exposure[ticker] = {"direct": 0.0, "via_etf": {}, "total": 0.0}
            exposure[ticker]["direct"] += weight
            exposure[ticker]["total"] += weight

    return exposure


def get_overlap_warnings(
    portfolio_weights: dict[str, float],
) -> list[dict]:
    """Find stocks held both directly AND through ETFs.

    Returns list of {ticker, direct_weight, etf_weight, total_weight, etfs}
    """
    exposure = compute_look_through(portfolio_weights)
    warnings = []

    for stock, info in exposure.items():
        if info["direct"] > 0 and info["via_etf"]:
            etf_total = sum(info["via_etf"].values())
            warnings.append({
                "ticker": stock,
                "direct_weight": info["direct"],
                "etf_weight": etf_total,
                "total_weight": info["direct"] + etf_total,
                "etfs": list(info["via_etf"].keys()),
            })

    warnings.sort(key=lambda x: x["total_weight"], reverse=True)
    return warnings


# ── Cache management ────────────────────────────────────────────────


def _load_cache() -> dict | None:
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            # Check if cache is less than 30 days old
            cached_date = datetime.fromisoformat(data.get("updated", "2000-01-01"))
            if (datetime.utcnow() - cached_date).days < 30:
                return data.get("etfs", {})
    except Exception as e:
        logger.warning(f"Failed to load ETF cache: {e}")
    return None


def save_cache(etf_data: dict[str, dict[str, float]]):
    """Save refreshed holdings to cache."""
    cache = {
        "updated": datetime.utcnow().isoformat(),
        "etfs": {
            ticker: {"holdings": holdings}
            for ticker, holdings in etf_data.items()
        },
    }
    try:
        _CACHE_FILE.write_text(json.dumps(cache, indent=2))
        logger.info(f"ETF holdings cache saved to {_CACHE_FILE}")
    except Exception as e:
        logger.warning(f"Failed to save ETF cache: {e}")
