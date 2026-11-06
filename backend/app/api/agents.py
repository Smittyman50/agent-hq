"""CRUD for agent definitions, plus import/export as JSON."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import AgentDef, ToolServer, _now

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentIn(BaseModel):
    name: str
    description: str = ""
    instructions: str = ""
    model: str = "ollama:gpt-oss:20b"
    tool_server_ids: list[str] = []
    delegates: list[str] = []
    desktop_url: str | None = None
    is_default: bool = False
    enabled: bool = True


def _clear_other_defaults(s: Session, keep_id: str) -> None:
    for a in s.exec(select(AgentDef).where(AgentDef.is_default == True)).all():  # noqa: E712
        if a.id != keep_id:
            a.is_default = False
            s.add(a)


def _check_name(s: Session, name: str, own_id: str | None = None) -> None:
    if not name.strip():
        raise HTTPException(422, "name is required")
    other = s.exec(select(AgentDef).where(AgentDef.name == name)).first()
    if other and other.id != own_id:
        raise HTTPException(409, f"an agent named '{name}' already exists")


@router.get("", response_model=list[AgentDef])
def list_agents(s: Session = Depends(get_session)):
    return s.exec(select(AgentDef).order_by(AgentDef.name)).all()


@router.post("", response_model=AgentDef, status_code=201)
def create_agent(body: AgentIn, s: Session = Depends(get_session)):
    _check_name(s, body.name)
    a = AgentDef(**body.model_dump())
    s.add(a)
    if a.is_default:
        _clear_other_defaults(s, a.id)
    s.commit()
    s.refresh(a)
    return a


@router.get("/export")
def export_agents(s: Session = Depends(get_session)) -> dict[str, Any]:
    """Portable JSON: delegates and tools are referenced by name, not ID."""
    agents = s.exec(select(AgentDef)).all()
    servers = s.exec(select(ToolServer)).all()
    by_id = {a.id: a.name for a in agents}
    srv_by_id = {t.id: t.name for t in servers}
    return {
        "tool_servers": [t.model_dump(exclude={"id", "created_at"}) for t in servers],
        "agents": [
            {**a.model_dump(exclude={"id", "created_at", "updated_at", "tool_server_ids", "delegates"}),
             "tools": [srv_by_id[i] for i in a.tool_server_ids if i in srv_by_id],
             "delegates": ["*" if d == "*" else by_id.get(d, d) for d in a.delegates]}
            for a in agents
        ],
    }


@router.post("/import")
def import_agents(body: dict[str, Any], s: Session = Depends(get_session)) -> dict[str, int]:
    """Create or update agents/tool servers from an export. Matches on name."""
    for t in body.get("tool_servers", []):
        existing = s.exec(select(ToolServer).where(ToolServer.name == t["name"])).first()
        row = existing or ToolServer(name=t["name"])
        for k, v in t.items():
            setattr(row, k, v)
        s.add(row)
    s.commit()
    servers = {t.name: t.id for t in s.exec(select(ToolServer)).all()}

    pending = []
    for a in body.get("agents", []):
        existing = s.exec(select(AgentDef).where(AgentDef.name == a["name"])).first()
        row = existing or AgentDef(name=a["name"])
        for k, v in a.items():
            if k not in ("tools", "delegates") and hasattr(row, k):
                setattr(row, k, v)
        row.tool_server_ids = [servers[n] for n in a.get("tools", []) if n in servers]
        row.updated_at = _now()
        s.add(row)
        pending.append((row, a.get("delegates", [])))
    s.commit()
    names = {a.name: a.id for a in s.exec(select(AgentDef)).all()}
    for row, delegates in pending:
        row.delegates = ["*" if d == "*" else names[d] for d in delegates if d == "*" or d in names]
        s.add(row)
    s.commit()
    return {"agents": len(pending), "tool_servers": len(body.get("tool_servers", []))}


@router.get("/{agent_id}", response_model=AgentDef)
def get_agent(agent_id: str, s: Session = Depends(get_session)):
    a = s.get(AgentDef, agent_id)
    if not a:
        raise HTTPException(404)
    return a


@router.put("/{agent_id}", response_model=AgentDef)
def update_agent(agent_id: str, body: AgentIn, s: Session = Depends(get_session)):
    a = s.get(AgentDef, agent_id)
    if not a:
        raise HTTPException(404)
    _check_name(s, body.name, agent_id)
    for k, v in body.model_dump().items():
        setattr(a, k, v)
    a.updated_at = _now()
    s.add(a)
    if a.is_default:
        _clear_other_defaults(s, a.id)
    s.commit()
    s.refresh(a)
    return a


@router.delete("/{agent_id}", status_code=204)
def delete_agent(agent_id: str, s: Session = Depends(get_session)):
    a = s.get(AgentDef, agent_id)
    if not a:
        raise HTTPException(404)
    # Disable instead of deleting so old conversations and jobs stay readable.
    a.enabled = False
    for other in s.exec(select(AgentDef)).all():
        if agent_id in other.delegates:
            other.delegates = [d for d in other.delegates if d != agent_id]
            s.add(other)
    s.add(a)
    s.commit()
