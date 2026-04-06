"""Tool registry for the agent — schemas + dispatch.

Portfolio tools (READONLY_TOOLS from nemoclaw.tools) execute server-side.
Chart tools are *queue-only*: the agent can call them but they execute on
the dashboard side after the response is returned (the dashboard owns
st.session_state, the agent does not). Queued chart actions are returned
to the caller in the API response.
"""

from __future__ import annotations

from typing import Any

from nemoclaw import tools as nc_tools


ACCOUNT_ENUM = ["sip", "ss_isa", "gia"]


PORTFOLIO_TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "get_portfolio",
        "description": "Get portfolio positions, weights and total value in GBP for one or all accounts.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM,
                        "description": "Optional account filter. Omit for all."},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_risk",
        "description": "Latest risk metrics (Sharpe, Sortino, volatility, max drawdown, CVaR).",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_risk_history",
        "description": "Risk metrics history over the last N days.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
            "days": {"type": "integer", "default": 30},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_value_history",
        "description": "Portfolio total value history over the last N days.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
            "days": {"type": "integer", "default": 30},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_trades",
        "description": "Recent trade/transaction history.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
            "limit": {"type": "integer", "default": 20},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_macro",
        "description": "Current macro indicators (VIX, gold, oil, US10Y, GBP/USD).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_drift",
        "description": "Drift of current portfolio from the last optimizer targets.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }},
    }},
    {"type": "function", "function": {
        "name": "get_news",
        "description": "Recent relevant financial news headlines.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "get_alerts",
        "description": "Smart price-move alerts for current holdings.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "search_ticker",
        "description": "Search for a stock/ETF by name or symbol.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "get_last_optimize",
        "description": "Most recent optimizer result from the database (cheap, no GPU).",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }},
    }},
    {"type": "function", "function": {
        "name": "look_through",
        "description": "Decompose ETFs into top-20 constituents to show TRUE stock exposure and overlap warnings.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }},
    }},
    {"type": "function", "function": {
        "name": "optimize",
        "description": "Run NVIDIA Mean-CVaR optimizer on DGX Spark. SLOW (10-30s).",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }},
    }},
    {"type": "function", "function": {
        "name": "consider",
        "description": "Evaluate a specific ticker — should Jeremy hold/add/increase it? SLOW (20-60s).",
        "parameters": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "account": {"type": "string", "enum": ACCOUNT_ENUM, "default": "sip"},
        }, "required": ["ticker"]},
    }},
    {"type": "function", "function": {
        "name": "stress_test",
        "description": "Run a pre-defined stress scenario. SLOW (20-40s).",
        "parameters": {"type": "object", "properties": {
            "scenario": {"type": "string",
                         "enum": ["gulf-war", "recession", "tech-crash", "rate-hike"]},
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
        }, "required": ["scenario"]},
    }},
    {"type": "function", "function": {
        "name": "frontier",
        "description": "Compute the efficient frontier. SLOW (60-300s).",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM},
            "num_points": {"type": "integer", "default": 12},
        }},
    }},
    {"type": "function", "function": {
        "name": "backtest",
        "description": "Backtest the current or optimal portfolio.",
        "parameters": {"type": "object", "properties": {
            "account": {"type": "string", "enum": ACCOUNT_ENUM, "default": "sip"},
            "use_optimal": {"type": "boolean", "default": False},
        }},
    }},
]


CHART_TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "chart_set_ticker",
        "description": "Switch the chart to a different ticker.",
        "parameters": {"type": "object",
                       "properties": {"ticker": {"type": "string"}},
                       "required": ["ticker"]},
    }},
    {"type": "function", "function": {
        "name": "chart_add_hline",
        "description": "Draw a horizontal price line on the chart (entry, target, stop).",
        "parameters": {"type": "object", "properties": {
            "price": {"type": "number"},
            "label": {"type": "string"},
            "color": {"type": "string", "default": "orange"},
        }, "required": ["price", "label"]},
    }},
    {"type": "function", "function": {
        "name": "chart_add_annotation",
        "description": "Place a dated text annotation on the chart.",
        "parameters": {"type": "object", "properties": {
            "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            "text": {"type": "string"},
        }, "required": ["date", "text"]},
    }},
    {"type": "function", "function": {
        "name": "chart_hypothetical_position",
        "description": "Mark a hypothetical buy/sell with shares and entry price.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["buy", "sell"]},
            "shares": {"type": "number"},
            "price": {"type": "number"},
            "note": {"type": "string"},
        }, "required": ["action", "shares", "price"]},
    }},
    {"type": "function", "function": {
        "name": "chart_clear_overlays",
        "description": "Remove all hypothetical overlays.",
        "parameters": {"type": "object", "properties": {}},
    }},
]


def get_tool_schemas(include_chart: bool = False) -> list[dict]:
    return list(PORTFOLIO_TOOL_SCHEMAS) + (list(CHART_TOOL_SCHEMAS) if include_chart else [])


def is_chart_tool(name: str) -> bool:
    return name.startswith("chart_")


def execute_portfolio_tool(name: str, args: dict) -> Any:
    fn = nc_tools.READONLY_TOOLS.get(name)
    if fn is None:
        raise ValueError(f"unknown portfolio tool: {name}")
    return fn(**(args or {}))


def queue_chart_action(name: str, args: dict) -> dict:
    """Build a chart action dict to be returned to the caller for client-side execution."""
    return {"type": name.removeprefix("chart_"), "args": args or {}}
