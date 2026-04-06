"""HTTP client for the cufolio optimizer API on DGX Spark."""

from __future__ import annotations

import logging

import requests

from nemoclaw import config

logger = logging.getLogger("nemoclaw.spark")

# Pre-defined stress scenarios (mirrors server/stress.py names)
STRESS_SCENARIOS = ["gulf-war", "recession", "tech-crash", "rate-hike"]

# Timeouts: (connect, read)
_TIMEOUT_OPTIMIZE = (10, 120)
_TIMEOUT_FRONTIER = (10, 300)
_TIMEOUT_BACKTEST = (10, 60)


def _url(path: str) -> str:
    base = config.SPARK_API_URL.rstrip("/")
    return f"{base}{path}"


def _headers() -> dict:
    return {"X-API-Key": config.SPARK_API_KEY, "Content-Type": "application/json"}


def _post(path: str, payload: dict, timeout=_TIMEOUT_OPTIMIZE) -> dict:
    """POST to the Spark optimizer API, return parsed JSON."""
    url = _url(path)
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        raise RuntimeError(f"Cannot reach Spark optimizer at {url} — is the service running?")
    except requests.Timeout:
        raise RuntimeError(f"Spark optimizer timed out ({timeout[1]}s) — try fewer scenarios")
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", "")
        except Exception:
            detail = e.response.text[:200]
        raise RuntimeError(f"Spark optimizer error ({e.response.status_code}): {detail}")


def health() -> dict:
    """Check Spark optimizer health."""
    url = _url("/health")
    resp = requests.get(url, headers=_headers(), timeout=(5, 10))
    resp.raise_for_status()
    return resp.json()


def optimize(
    tickers: list[str],
    risk_aversion: float = 1.0,
    w_min: float = 0.0,
    w_max: float = 1.0,
    c_min: float = 0.0,
    c_max: float = 0.05,
    num_scenarios: int = 10_000,
    existing_weights: dict | None = None,
    cardinality: int | None = None,
    w_min_override: dict | None = None,
    w_max_override: dict | None = None,
    etf_constituents: dict | None = None,
) -> dict:
    """Run CVaR optimisation on Spark. Returns weights + metrics."""
    payload = {
        "tickers": tickers,
        "risk_aversion": risk_aversion,
        "w_min": w_min,
        "w_max": w_max,
        "c_min": c_min,
        "c_max": c_max,
        "num_scenarios": num_scenarios,
        "solver": "auto",
    }
    if existing_weights:
        payload["existing_weights"] = existing_weights
    if cardinality is not None:
        payload["cardinality"] = cardinality
    if w_min_override:
        payload["w_min_override"] = w_min_override
    if w_max_override:
        payload["w_max_override"] = w_max_override
    if etf_constituents:
        payload["etf_constituents"] = etf_constituents
    return _post("/optimize", payload)


def stress_test(
    tickers: list[str],
    scenario_name: str | None = None,
    shocks: dict | None = None,
    shock_scenario_weight: float = 0.25,
    risk_aversion: float = 1.0,
    w_min: float = 0.0,
    w_max: float = 1.0,
    num_scenarios: int = 10_000,
) -> dict:
    """Run stress test on Spark. Returns base vs stress comparison."""
    payload = {
        "tickers": tickers,
        "shocks": shocks or {},
        "shock_scenario_weight": shock_scenario_weight,
        "risk_aversion": risk_aversion,
        "w_min": w_min,
        "w_max": w_max,
        "num_scenarios": num_scenarios,
        "solver": "auto",
    }
    if scenario_name:
        payload["scenario_name"] = scenario_name
    return _post("/stress-test", payload)


def frontier(
    tickers: list[str],
    num_points: int = 15,
    w_min: float = 0.0,
    w_max: float = 1.0,
    num_scenarios: int = 5_000,
) -> dict:
    """Compute efficient frontier on Spark."""
    payload = {
        "tickers": tickers,
        "num_points": num_points,
        "w_min": w_min,
        "w_max": w_max,
        "num_scenarios": num_scenarios,
        "solver": "auto",
    }
    return _post("/frontier", payload, timeout=_TIMEOUT_FRONTIER)


def backtest(
    tickers: list[str],
    weights: dict[str, float],
    cash: float = 0.0,
    risk_free_rate: float = 0.0,
) -> dict:
    """Backtest a portfolio on Spark."""
    payload = {
        "tickers": tickers,
        "weights": weights,
        "cash": cash,
        "risk_free_rate": risk_free_rate,
    }
    return _post("/backtest", payload, timeout=_TIMEOUT_BACKTEST)
