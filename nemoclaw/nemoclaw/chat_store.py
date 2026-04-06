"""Chat persistence — shared across Telegram and dashboard surfaces.

Stores chat sessions and messages in the same portfolio.db so any client
(Telegram, dashboard, future surfaces) can see and continue conversations.
Each session is tagged with a `source` (telegram | dashboard | other).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from nemoclaw.db import get_conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'dashboard',  -- telegram | dashboard | other
    page_context TEXT,                          -- where it was started (page name, telegram chat id, etc)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    role TEXT NOT NULL,               -- system | user | assistant | tool
    content TEXT,
    tool_calls TEXT,                  -- JSON list of tool calls (assistant role)
    tool_call_id TEXT,                -- id of the call this responds to (tool role)
    tool_name TEXT,                   -- name of tool that produced this result (tool role)
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_source ON chat_sessions(source, updated_at DESC);
"""


def init_chat_schema() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ── Sessions ──────────────────────────────────────────────────────────

def create_session(
    title: str = "New chat",
    source: str = "dashboard",
    page_context: Optional[str] = None,
) -> int:
    init_chat_schema()
    now = _now()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_sessions (title, source, page_context, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, source, page_context, now, now),
        )
        return cur.lastrowid


def list_sessions(source: Optional[str] = None, limit: int = 50) -> list[dict]:
    init_chat_schema()
    with get_conn() as conn:
        if source:
            rows = conn.execute(
                "SELECT s.id, s.title, s.source, s.page_context, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS message_count "
                "FROM chat_sessions s WHERE source=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT s.id, s.title, s.source, s.page_context, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS message_count "
                "FROM chat_sessions s ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_session(session_id: int) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
    return dict(row) if row else None


def rename_session(session_id: int, title: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
            (title, _now(), session_id),
        )


def delete_session(session_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))


# ── Messages ──────────────────────────────────────────────────────────

def add_message(
    session_id: int,
    role: str,
    content: Optional[str] = None,
    tool_calls: Optional[list[dict]] = None,
    tool_call_id: Optional[str] = None,
    tool_name: Optional[str] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages "
            "(session_id, role, content, tool_calls, tool_call_id, tool_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                tool_name,
                _now(),
            ),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at=? WHERE id=?",
            (_now(), session_id),
        )
        return cur.lastrowid


def get_messages(session_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("tool_calls"):
            try:
                d["tool_calls"] = json.loads(d["tool_calls"])
            except Exception:
                d["tool_calls"] = None
        out.append(d)
    return out


def to_openai_messages(session_id: int) -> list[dict]:
    """Convert stored messages to vLLM/OpenAI chat format."""
    msgs = get_messages(session_id)
    out = []
    for m in msgs:
        role = m["role"]
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id") or "",
                "name": m.get("tool_name") or "",
                "content": m.get("content") or "",
            })
        elif role == "assistant" and m.get("tool_calls"):
            out.append({
                "role": "assistant",
                "content": m.get("content") or "",
                "tool_calls": m["tool_calls"],
            })
        else:
            out.append({"role": role, "content": m.get("content") or ""})
    return out
