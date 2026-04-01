"""LLM client — talks to vLLM on DGX Spark."""

import logging
from openai import OpenAI
from . import config

logger = logging.getLogger("nemoclaw.llm")

SYSTEM_PROMPT = """You are NemoClaw, Jeremy's personal financial analyst agent. You monitor his Freetrade portfolios (SIP/SIPP and SS ISA) and provide concise, actionable insights.

Key context:
- Jeremy is a UK-based retail investor using Freetrade
- SIP (SIPP) is his pension — should be more conservative
- SS ISA is his stocks & shares ISA — can be more aggressive
- All values should be in GBP
- UK stocks trade on LSE with .L suffix, prices in pence (GBX) — always convert to GBP for display
- Jeremy prefers direct, no-nonsense analysis — get to the point

When analysing trades or market moves:
- Focus on what it means for HIS specific portfolio
- Flag concentration risks (his SIP is heavily weighted to RR.L)
- Consider macro factors (Middle East tensions, trade policy, interest rates)
- Keep responses concise — this is Telegram, not a report"""


def chat(messages, temperature=0.3, max_tokens=1000):
    """Send messages to vLLM and return the response text."""
    try:
        client = OpenAI(base_url=config.VLLM_BASE_URL, api_key="not-needed")
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages
        response = client.chat.completions.create(
            model=config.VLLM_MODEL,
            messages=full_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return f"LLM unavailable: {e}"
