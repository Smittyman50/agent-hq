"""Registry of MCP tool servers. Register one here, then tick it on any agent."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

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
    enabled: bool = True


@router.get("", response_model=list[ToolServer])
def list_servers(s: Session = Depends(get_session)):
    return s.exec(select(ToolServer).order_by(ToolServer.name)).all()


@router.post("", response_model=ToolServer, status_code=201)
def create_server(body: ToolServerIn, s: Session = Depends(get_session)):
    if s.exec(select(ToolServer).where(ToolServer.name == body.name)).first():
        raise HTTPException(409, f"a tool server named '{body.name}' already exists")
    t = ToolServer(**body.model_dump())
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


@router.put("/{server_id}", response_model=ToolServer)
def update_server(server_id: str, body: ToolServerIn, s: Session = Depends(get_session)):
    t = s.get(ToolServer, server_id)
    if not t:
        raise HTTPException(404)
    for k, v in body.model_dump().items():
        setattr(t, k, v)
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
        return [{"name": x.name, "description": x.description} for x in tools]
    except Exception as e:
        raise HTTPException(502, f"could not reach {t.name}: {e}") from e
