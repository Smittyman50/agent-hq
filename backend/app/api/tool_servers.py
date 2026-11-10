"""Registry of MCP tool servers. Register one here, then tick it on any agent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from .. import approvals
from ..db import get_session
from ..models import AgentDef, ToolServer, Transport
from ..runtime import mcp_toolset

router = APIRouter(prefix="/api/tool-servers", tags=["tools"])


class ToolServerIn(BaseModel):
    name: str
    description: str = ""
    transport: Transport = Transport.http
    url: str | None = None
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    prefix_tools: bool = True
    desktop: bool = False
    # {tool name or "*": "allow" | "ask" | "block"}. None (left out) keeps the current rules.
    approval_rules: dict[str, str] | None = None
    eval_live: dict[str, bool] | None = None  # tools safe to run for real in tests
    enabled: bool = True


def _check_rules(rules: dict[str, str] | None) -> None:
    for tool, mode in (rules or {}).items():
        if mode not in approvals.MODES:
            raise HTTPException(422, f"approval for {tool} must be one of {', '.join(approvals.MODES)}")


@router.get("", response_model=list[ToolServer])
def list_servers(s: Session = Depends(get_session)):
    return s.exec(select(ToolServer).order_by(ToolServer.name)).all()


@router.post("", response_model=ToolServer, status_code=201)
def create_server(body: ToolServerIn, s: Session = Depends(get_session)):
    if s.exec(select(ToolServer).where(ToolServer.name == body.name)).first():
        raise HTTPException(409, f"a tool server named '{body.name}' already exists")
    _check_rules(body.approval_rules)
    t = ToolServer(**{**body.model_dump(), "approval_rules": body.approval_rules or {},
                      "eval_live": body.eval_live or {}})
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


@router.put("/{server_id}", response_model=ToolServer)
def update_server(server_id: str, body: ToolServerIn, s: Session = Depends(get_session)):
    t = s.get(ToolServer, server_id)
    if not t:
        raise HTTPException(404)
    _check_rules(body.approval_rules)
    for k, v in body.model_dump(exclude={"approval_rules", "eval_live"}).items():
        setattr(t, k, v)
    if body.approval_rules is not None:
        t.approval_rules = {k: v for k, v in body.approval_rules.items() if v}
    if body.eval_live is not None:
        t.eval_live = {k: bool(v) for k, v in body.eval_live.items()}
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


@router.delete("/{server_id}", status_code=204)
def delete_server(server_id: str, s: Session = Depends(get_session)):
    t = s.get(ToolServer, server_id)
    if not t:
        raise HTTPException(404)
    for a in s.exec(select(AgentDef)).all():
        if server_id in a.tool_server_ids:
            a.tool_server_ids = [i for i in a.tool_server_ids if i != server_id]
            s.add(a)
    s.delete(t)
    s.commit()


@router.get("/{server_id}/tools")
async def probe_server(server_id: str, s: Session = Depends(get_session)):
    """Connect to the server and list its tools (the UI's 'Test' button)."""
    t = s.get(ToolServer, server_id)
    if not t:
        raise HTTPException(404)
    try:
        ts = mcp_toolset(t)
        async with ts:
            tools = await ts.client.list_tools()
        out = []
        for x in tools:
            hint = approvals.suggested(getattr(x, "meta", None))
            ann = getattr(x, "annotations", None)
            read_only = bool(ann and getattr(ann, "readOnlyHint", None))
            out.append({"name": x.name, "description": x.description, "suggested_approval": hint,
                        "approval": approvals.mode_for(t.approval_rules, x.name, getattr(x, "meta", None)),
                        "read_only_hint": read_only,
                        "safe_in_tests": (t.eval_live or {}).get(x.name, read_only)})
        return out
    except Exception as e:
        raise HTTPException(502, f"could not reach {t.name}: {e}") from e
