"""FastAPI HTTP edge for JezFinanceClaw.

Exposes agent chat sessions AND portfolio data to JezFinancialDashboard.
Auth via X-API-Key header (key set via AGENT_API_KEY env var).

Agent endpoints:
  GET    /agent/health
  POST   /agent/sessions
  GET    /agent/sessions
  GET    /agent/sessions/{id}
  PATCH  /agent/sessions/{id}
  DELETE /agent/sessions/{id}
  GET    /agent/sessions/{id}/messages
  POST   /agent/sessions/{id}/turn

Portfolio data endpoints:
  GET    /api/health
  GET    /api/portfolios
  GET    /api/portfolios/{pid}/positions
  GET    /api/portfolios/{pid}/risk
  GET    /api/portfolios/{pid}/risk_history
  GET    /api/tickers
  GET    /api/prices/{ticker}

Run standalone:
  uvicorn jezclaw.agent_api:app --host 0.0.0.0 --port 18792
"""

from __future__ import annotations

import os
import time
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from jezclaw import agent_core, chat_store, db, portfolio as _pf
from jezclaw.agent_tools import PORTFOLIO_TOOL_SCHEMAS, CHART_TOOL_SCHEMAS


API_KEY = os.environ.get("AGENT_API_KEY", "")


def require_key(x_api_key: Optional[str] = Header(None)):
    if not API_KEY:
        # If no key configured, allow (loopback only — host_network on Pi).
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


app = FastAPI(title="JezFinanceClaw Agent API", version="1.0")


# ── Schemas ──────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    title: Optional[str] = "New chat"
    source: str = "dashboard"
    page_context: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: int
    title: str
    source: str


class SessionSummary(BaseModel):
    id: int
    title: str
    source: str
    page_context: Optional[str] = None
    created_at: str
    updated_at: str
    message_count: int = 0


class RenameRequest(BaseModel):
    title: str


class TurnRequest(BaseModel):
    message: str
    page: Optional[str] = None
    page_context: Optional[dict] = None
    source: str = "dashboard"
    auto_title_if_first: bool = True


class ToolCallLog(BaseModel):
    name: str
    args: dict
    ok: bool
    summary: str


class ChartAction(BaseModel):
    type: str
    args: dict


class TurnResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallLog] = Field(default_factory=list)
    chart_actions: list[ChartAction] = Field(default_factory=list)
    iterations: int
    duration_ms: int


# ── Health ───────────────────────────────────────────────────────────

@app.get("/agent/health")
def health():
    chat_store.init_chat_schema()
    return {
        "ok": True,
        "portfolio_tools": len(PORTFOLIO_TOOL_SCHEMAS),
        "chart_tools": len(CHART_TOOL_SCHEMAS),
        "auth_required": bool(API_KEY),
    }


# ── Sessions ─────────────────────────────────────────────────────────

@app.post("/agent/sessions", response_model=CreateSessionResponse,
          dependencies=[Depends(require_key)])
def create_session(req: CreateSessionRequest):
    sid = chat_store.create_session(
        title=req.title or "New chat",
        source=req.source,
        page_context=req.page_context,
    )
    return CreateSessionResponse(session_id=sid, title=req.title or "New chat", source=req.source)


@app.get("/agent/sessions", response_model=list[SessionSummary],
         dependencies=[Depends(require_key)])
def list_sessions(source: Optional[str] = None, limit: int = 50):
    return chat_store.list_sessions(source=source, limit=limit)


@app.get("/agent/sessions/{session_id}", dependencies=[Depends(require_key)])
def get_session(session_id: int):
    s = chat_store.get_session(session_id)
    if not s:
        raise HTTPException(404, "session not found")
    return s


@app.patch("/agent/sessions/{session_id}", dependencies=[Depends(require_key)])
def rename_session(session_id: int, req: RenameRequest):
    if not chat_store.get_session(session_id):
        raise HTTPException(404, "session not found")
    chat_store.rename_session(session_id, req.title)
    return {"ok": True}


@app.delete("/agent/sessions/{session_id}", dependencies=[Depends(require_key)])
def delete_session(session_id: int):
    chat_store.delete_session(session_id)
    return {"ok": True}


@app.get("/agent/sessions/{session_id}/messages", dependencies=[Depends(require_key)])
def get_messages(session_id: int):
    if not chat_store.get_session(session_id):
        raise HTTPException(404, "session not found")
    return {"messages": chat_store.get_messages(session_id)}


# ── Turn ─────────────────────────────────────────────────────────────

@app.post("/agent/sessions/{session_id}/turn", response_model=TurnResponse,
          dependencies=[Depends(require_key)])
def turn(session_id: int, req: TurnRequest):
    session = chat_store.get_session(session_id)
    if not session:
        raise HTTPException(404, "session not found")

    # Auto-title on first message if still default
    is_first = len(chat_store.get_messages(session_id)) == 0
    if req.auto_title_if_first and is_first and session["title"] in ("New chat", "", None):
        try:
            new_title = agent_core.auto_title(req.message)
            chat_store.rename_session(session_id, new_title)
        except Exception:
            pass

    t0 = time.time()
    result = agent_core.run_turn(
        session_id=session_id,
        user_message=req.message,
        source=req.source,
        page=req.page,
        page_context=req.page_context,
    )
    duration_ms = int((time.time() - t0) * 1000)

    return TurnResponse(
        reply=result["reply"],
        tool_calls=result["tool_calls"],
        chart_actions=result["chart_actions"],
        iterations=result["iterations"],
        duration_ms=duration_ms,
    )


# ── Portfolio data (consumed by JezFinancialDashboard) ────────────────


@app.get("/api/health")
def data_health():
    return {"ok": True, "service": "JezFinanceClaw"}


@app.get("/api/portfolios")
def list_portfolios():
    """List all portfolios with total value and holdings count."""
    rows = db.get_portfolios()
    result = []
    for p in rows:
        try:
            positions, total = _pf.get_portfolio_summary(p["id"])
            n = len(positions)
        except Exception:
            total, n = 0, 0
        result.append({
            "id": p["id"],
            "name": p["name"],
            "total_value": round(total, 2),
            "n_holdings": n,
        })
    result.sort(key=lambda x: -x["total_value"])
    return result


@app.get("/api/portfolios/{pid}/positions")
def get_positions(pid: str):
    """Holdings with current prices and weights."""
    positions, total = _pf.get_portfolio_summary(pid)
    for r in positions:
        r["weight"] = round(r.get("weight", 0), 4)
        r["market_value"] = round(r.get("market_value", 0), 2)
    return positions


@app.get("/api/portfolios/{pid}/risk")
def get_risk(pid: str):
    """Latest risk metrics for a portfolio."""
    m = db.get_latest_risk_metrics(pid)
    return m if m else {}


@app.get("/api/portfolios/{pid}/risk_history")
def get_risk_history(pid: str, days: int = 90):
    """Risk metrics time series."""
    return db.get_risk_history(pid, days=days)


@app.get("/api/tickers")
def list_tickers():
    """All tickers held across portfolios."""
    tickers = db.get_all_tickers()
    return [{"ticker": t, "company": t} for t in tickers]


@app.get("/api/prices/{ticker}")
def get_prices(ticker: str, days: int = 365):
    """Daily close prices for a ticker."""
    from datetime import datetime, timedelta
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT date, close FROM prices WHERE ticker=? AND date>=? ORDER BY date",
            (ticker, start),
        ).fetchall()
    if not rows:
        return {"ticker": ticker, "currency": None, "points": []}
    is_pence = ticker.upper().endswith(".L")
    ccy = "GBP" if is_pence else "USD"
    points = [
        {"time": r["date"], "value": (r["close"] / 100.0) if is_pence else r["close"]}
        for r in rows
    ]
    return {"ticker": ticker, "currency": ccy, "points": points}


# ── Risk-page market stance (one-shot, structured) ───────────────────

@app.get("/agent/stance/{portfolio_id}", dependencies=[Depends(require_key)])
def stance(portfolio_id: str):
    try:
        return agent_core.get_stance(portfolio_id)
    except Exception as e:
        raise HTTPException(500, f"stance generation failed: {e}")
