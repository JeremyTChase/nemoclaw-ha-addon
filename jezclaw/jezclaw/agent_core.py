"""Agent core — vLLM function-calling loop shared by all surfaces.

Used by:
- agent_api.py (HTTP edge for the dashboard)
- (eventually) bot.py for Telegram, replacing the OpenClaw-skill pathway

Per turn:
  1. Persist user message to chat_store
  2. Build system prompt (persona + page/source context)
  3. Send to vLLM with tool schemas
  4. If response has tool_calls:
       - Portfolio tools execute server-side via jezclaw.tools
       - Chart tools are queued (executed client-side by the dashboard)
       - Persist tool results, loop
  5. Otherwise persist final assistant message and return
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from openai import OpenAI

from jezclaw import chat_store, config, tools as nc_tools
from jezclaw.agent_tools import (
    execute_portfolio_tool,
    get_tool_schemas,
    is_chart_tool,
    queue_chart_action,
)

logger = logging.getLogger("jezclaw.agent_core")


SYSTEM_BASE = """You are JezFinanceClaw, Jeremy's personal portfolio analyst.

CONTEXT:
- Jeremy is a UK retail investor with three accounts: SIP (SIPP, ~£500k, conservative pension), SS ISA (~£10k, aggressive), GIA (IBKR active trading).
- Values in GBP. UK tickers (.L) trade in pence — always convert.
- Biggest position: Rolls-Royce (RR.L) in SIPP.
- NVIDIA cufolio optimizer runs on DGX Spark — use it for mathematical answers, never opinion.

TOOL USE RULES:
- ALWAYS call tools for facts. Never answer portfolio questions from memory.
- Fast tools (get_portfolio, get_risk, look_through, get_drift, get_macro, get_trades, get_news, get_alerts) — use freely.
- Slow tools (optimize, consider, stress_test, frontier) hit the GPU — 10-120s. Only call when the user actually wants that analysis.
- For exposure/overlap → look_through first.
- For "what do you think of X" → consider <X>.
- For rebalancing → optimize.
- For scenario questions → stress_test.
- BUDGET: at most ~6 tool calls per turn. Pick the minimum set that answers the question.
- Do NOT pre-emptively run optimize/stress_test/frontier unless the user asked for that exact analysis.
- After ~3-4 tool calls, you should already be writing the final answer. Stop calling tools and reply.
- A simple greeting like "hello" requires ZERO tool calls — just greet back.

STYLE:
- Direct, no-nonsense. Flag concentration risks.
- Ground every claim in tool output.
- When citing optimizer results, prefix with "NVIDIA optimizer recommends...".
- You CANNOT execute trades from the dashboard surface. Tell Jeremy to use Telegram or Quick Trade for execution.
"""


def _client() -> OpenAI:
    return OpenAI(base_url=config.VLLM_BASE_URL, api_key="not-needed")


def _build_system_prompt(
    source: str,
    page: Optional[str],
    page_context: Optional[dict],
) -> str:
    parts = [SYSTEM_BASE.strip(), f"\nSURFACE: {source}"]
    if page:
        parts.append(f"PAGE: {page}")
    if page_context:
        try:
            parts.append("PAGE STATE:\n" + json.dumps(page_context, default=str, indent=2))
        except Exception:
            pass
    if page == "charting":
        parts.append(
            "Chart tools (chart_set_ticker, chart_add_hline, chart_add_annotation, "
            "chart_hypothetical_position, chart_clear_overlays) are available — use them "
            "to visualise hypotheses on Jeremy's chart. They are queued and applied "
            "on the dashboard side after your response."
        )
    return "\n\n".join(parts)


def run_turn(
    session_id: int,
    user_message: str,
    source: str = "dashboard",
    page: Optional[str] = None,
    page_context: Optional[dict] = None,
    max_iterations: int = 24,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> dict:
    """Run a single user turn through the agent loop.

    Returns: {
      reply: str,
      tool_calls: [{name, args, ok, summary}],
      chart_actions: [{type, args}],
      iterations: int,
    }
    """
    chat_store.add_message(session_id, "user", content=user_message)

    client = _client()
    system_prompt = _build_system_prompt(source, page, page_context)
    include_chart = page == "charting"
    tool_schemas = get_tool_schemas(include_chart=include_chart)

    tool_call_log: list[dict] = []
    chart_actions: list[dict] = []

    for iteration in range(max_iterations):
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(chat_store.to_openai_messages(session_id))

        try:
            response = client.chat.completions.create(
                model=config.VLLM_MODEL,
                messages=messages,
                tools=tool_schemas,
                tool_choice="auto",
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.exception("vLLM call failed")
            err = f"LLM call failed: {e}"
            chat_store.add_message(session_id, "assistant", content=err)
            return {"reply": err, "tool_calls": tool_call_log,
                    "chart_actions": chart_actions, "iterations": iteration}

        choice = response.choices[0].message
        tool_calls = getattr(choice, "tool_calls", None) or []

        if not tool_calls:
            text = choice.content or ""
            chat_store.add_message(session_id, "assistant", content=text)
            return {"reply": text, "tool_calls": tool_call_log,
                    "chart_actions": chart_actions, "iterations": iteration + 1}

        # Persist assistant-with-tool_calls
        serialised_calls = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments or "{}",
                },
            }
            for tc in tool_calls
        ]
        chat_store.add_message(
            session_id, "assistant",
            content=choice.content or "",
            tool_calls=serialised_calls,
        )

        # Execute / queue each call
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}

            if is_chart_tool(name):
                if not include_chart:
                    result = {"error": "chart tools are only available on the charting page"}
                    ok = False
                else:
                    action = queue_chart_action(name, args)
                    chart_actions.append(action)
                    result = {"queued": True, "action": action}
                    ok = True
            else:
                try:
                    result = execute_portfolio_tool(name, args)
                    ok = True
                except Exception as e:
                    logger.exception("tool %s failed", name)
                    result = {"error": str(e)}
                    ok = False

            result_text = json.dumps(result, default=str)[:12000]
            chat_store.add_message(
                session_id, "tool",
                content=result_text,
                tool_call_id=tc.id,
                tool_name=name,
            )
            tool_call_log.append({
                "name": name,
                "args": args,
                "ok": ok,
                "summary": _summarise_result(name, result),
            })

    fallback = "Hit the tool-call iteration limit — try a narrower question."
    chat_store.add_message(session_id, "assistant", content=fallback)
    return {"reply": fallback, "tool_calls": tool_call_log,
            "chart_actions": chart_actions, "iterations": max_iterations}


def _summarise_result(name: str, result) -> str:
    """Tiny human-readable summary for the per-tool-call log shown in the UI."""
    if isinstance(result, dict):
        if "error" in result:
            return f"error: {result['error']}"
        if "accounts" in result:
            return f"{len(result['accounts'])} account(s)"
        if "results" in result:
            return f"{len(result['results'])} results"
        if "trades" in result:
            return f"{len(result['trades'])} trades"
        if "indicators" in result:
            return f"{len(result['indicators'])} indicators"
        if "queued" in result:
            return "queued for chart"
    return "ok"


def _parse_json_loose(raw: str) -> dict:
    """Best-effort JSON parsing for LLM outputs.

    Strategy: try strict json.loads first, then strip code fences,
    then locate the first { and matching last }, then escape any
    raw newlines inside string literals as a last resort.
    """
    if not raw:
        raise ValueError("empty LLM response")
    s = raw.strip()
    # Strip markdown code fences
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Locate outermost JSON object
    first = s.find("{")
    last = s.rfind("}")
    if first >= 0 and last > first:
        candidate = s[first:last + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            # Last resort: try replacing literal newlines inside strings
            try:
                import re as _re
                fixed = _re.sub(r'(?<!\\)\n(?=[^"]*"(?:[^"\\]|\\.)*$)', r'\\n', candidate)
                return json.loads(fixed)
            except Exception:
                raise ValueError(f"could not parse JSON from LLM response: {e}; raw[:300]={raw[:300]}")
    raise ValueError(f"no JSON object in LLM response; raw[:300]={raw[:300]}")


# ── Market stance (one-shot structured output for Risk page) ─────────

STANCE_SYSTEM = """You are JezFinanceClaw producing a single risk-stance assessment
for a UK retail portfolio. Reply ONLY with a JSON object — no prose, no markdown.

Schema:
{
  "stance": "bullish" | "bearish" | "neutral" | "cautious",
  "confidence": "low" | "medium" | "high",
  "timeframe": "short-term" | "medium-term" | "long-term",
  "headline": "<one short sentence — max 90 chars>",
  "reasoning": ["<bullet>", "<bullet>", "<bullet>"],
  "key_risks": ["<bullet>", "<bullet>"],
  "metrics": {
    "volatility":   {"definition": "<1 sentence plain English>", "verdict": "<1-2 sentences in the context of THIS portfolio>", "tone": "good"|"warn"|"bad"|"neutral"},
    "sharpe":       {"definition": "...", "verdict": "...", "tone": "..."},
    "sortino":      {"definition": "...", "verdict": "...", "tone": "..."},
    "max_drawdown": {"definition": "...", "verdict": "...", "tone": "..."},
    "cvar":         {"definition": "...", "verdict": "...", "tone": "..."}
  }
}

Stance guide:
- bullish = expect portfolio to rise / risks declining
- bearish = expect portfolio to fall / risks rising
- cautious = mixed signals, lean defensive
- neutral = balanced

For each metric verdict: reference Jeremy's actual numbers and his account type
(SIPP = conservative pension; SS_ISA = aggressive small ISA). Use "good" tone when
the metric is healthy for that account, "warn" when borderline, "bad" when concerning.

Ground every claim in the data. Be direct, no hedging waffle.
"""


def get_stance(portfolio_id: str) -> dict:
    """One-shot LLM call that produces a structured bullish/bearish verdict.

    Pulls live risk + macro data from tools.py, sends to vLLM with a
    JSON-only prompt, parses and returns the structured object.
    """
    try:
        risk_now = nc_tools.get_risk(account=portfolio_id)
    except Exception as e:
        risk_now = {"error": str(e)}
    try:
        risk_hist = nc_tools.get_risk_history(account=portfolio_id, days=30)
    except Exception as e:
        risk_hist = {"error": str(e)}
    try:
        macro = nc_tools.get_macro()
    except Exception as e:
        macro = {"error": str(e)}
    try:
        portfolio = nc_tools.get_portfolio(account=portfolio_id)
    except Exception as e:
        portfolio = {"error": str(e)}

    user_payload = {
        "portfolio_id": portfolio_id,
        "risk_metrics_latest": risk_now,
        "risk_history_30d": risk_hist,
        "macro": macro,
        "portfolio_summary": portfolio,
    }

    # Strict JSON schema for vLLM guided generation
    metric_obj = {
        "type": "object",
        "properties": {
            "definition": {"type": "string"},
            "verdict": {"type": "string"},
            "tone": {"type": "string", "enum": ["good", "warn", "bad", "neutral"]},
        },
        "required": ["definition", "verdict", "tone"],
    }
    stance_schema = {
        "type": "object",
        "properties": {
            "stance": {"type": "string", "enum": ["bullish", "bearish", "neutral", "cautious"]},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "timeframe": {"type": "string", "enum": ["short-term", "medium-term", "long-term"]},
            "headline": {"type": "string"},
            "reasoning": {"type": "array", "items": {"type": "string"}},
            "key_risks": {"type": "array", "items": {"type": "string"}},
            "metrics": {
                "type": "object",
                "properties": {
                    "volatility":   metric_obj,
                    "sharpe":       metric_obj,
                    "sortino":      metric_obj,
                    "max_drawdown": metric_obj,
                    "cvar":         metric_obj,
                },
                "required": ["volatility", "sharpe", "sortino", "max_drawdown", "cvar"],
            },
        },
        "required": ["stance", "confidence", "timeframe", "headline", "reasoning", "key_risks", "metrics"],
    }

    client = _client()
    resp = client.chat.completions.create(
        model=config.VLLM_MODEL,
        messages=[
            {"role": "system", "content": STANCE_SYSTEM},
            {"role": "user",
             "content": "Assess the stance for this portfolio.\n\nDATA:\n"
                        + json.dumps(user_payload, default=str)[:14000]},
        ],
        temperature=0.2,
        max_tokens=1500,
        extra_body={"guided_json": stance_schema},
    )
    raw = (resp.choices[0].message.content or "").strip()
    parsed = _parse_json_loose(raw)
    parsed["_meta"] = {
        "portfolio_id": portfolio_id,
        "data_freshness": {
            "risk_now": bool(risk_now and "error" not in risk_now),
            "macro": bool(macro and "error" not in macro),
            "history_points": len(risk_hist) if isinstance(risk_hist, list) else 0,
        },
    }
    return parsed


# ── Auto-title helper ────────────────────────────────────────────────

def auto_title(first_user_message: str) -> str:
    try:
        client = _client()
        resp = client.chat.completions.create(
            model=config.VLLM_MODEL,
            messages=[
                {"role": "system",
                 "content": "You produce 3-6 word chat titles. No quotes, no trailing punctuation."},
                {"role": "user",
                 "content": f"Title for a chat that starts with: {first_user_message[:300]}"},
            ],
            temperature=0.2,
            max_tokens=20,
        )
        title = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
        return title[:60] if title else (first_user_message[:40] or "New chat")
    except Exception:
        return first_user_message[:40] or "New chat"
