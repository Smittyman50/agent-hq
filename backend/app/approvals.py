"""Tool approvals: decide per tool whether agents may call it, must ask you, or can't.

Rules live on each tool server (Settings → Tools → Approvals), keyed by tool name, with
"*" for the server's default:

* **allow**: the call just runs (the default for tools with no rule).
* **ask**: the call waits for you. An approval card appears in the conversation and in
  the Requests inbox (and on MQTT). Approve, approve and don't ask again for a while in
  that conversation, or deny with a note. If nobody decides within
  AHQ_APPROVAL_TIMEOUT_MINUTES (default 30), the call isn't made and the agent is told so.
* **block**: the call is refused and the agent is told it isn't allowed.

A server can suggest a mode for each of its tools in the tool's MCP metadata
(`{"agent_hq/approval": "ask"}`); the workbench does this for tools that run code. Your
rules always win. Sub-agents follow the same rules as everyone else.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from sqlmodel import func, select

from . import db
from .models import ToolApproval, ToolServer

MODES = ("allow", "ask", "block")
META_KEY = "agent_hq/approval"


def timeout_seconds() -> float:
    return float(os.environ.get("AHQ_APPROVAL_TIMEOUT_MINUTES", "30")) * 60


def suggested(meta: dict[str, Any] | None) -> str | None:
    mode = (meta or {}).get(META_KEY)
    return mode if mode in MODES else None


def mode_for(rules: dict[str, str] | None, tool: str, meta: dict[str, Any] | None = None) -> str:
    """The mode for one tool: its own rule, else the server's "*" rule, else the server's
    suggestion, else allow."""
    rules = rules or {}
    for key in (tool, "*"):
        if rules.get(key) in MODES:
            return rules[key]
    return suggested(meta) or "allow"


def key(approval_id: str) -> str:
    return f"ahq:approval:{approval_id}"


def remembered(agent_id: str, thread_id: str, server_id: str, tool: str) -> bool:
    """You approved this tool for this agent and conversation, and asked not to be asked again."""
    now = datetime.now(timezone.utc)
    with db.session() as s:
        rows = s.exec(select(ToolApproval).where(
            ToolApproval.agent_id == agent_id, ToolApproval.thread_id == thread_id,
            ToolApproval.server_id == server_id, ToolApproval.tool == tool,
            ToolApproval.status == "approved", ToolApproval.remember_until.is_not(None))).all()
    return any((r.remember_until if r.remember_until.tzinfo else
                r.remember_until.replace(tzinfo=timezone.utc)) > now for r in rows)


def pending_count() -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(ToolApproval)
                      .where(ToolApproval.status == "pending")).one()


def server(server_id: str) -> ToolServer | None:
    with db.session() as s:
        return s.get(ToolServer, server_id)


def sweep() -> int:
    """Mark approvals nobody decided in time as expired (e.g. after a worker restart)."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds() + 60)
    n = 0
    with db.session() as s:
        for r in s.exec(select(ToolApproval).where(ToolApproval.status == "pending")).all():
            created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=timezone.utc)
            if created < cutoff:
                r.status, r.decided_at = "expired", datetime.now(timezone.utc)
                s.add(r)
                n += 1
        s.commit()
    return n
