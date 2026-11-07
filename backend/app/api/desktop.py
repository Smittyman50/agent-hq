"""Taking control of an agent's desktop, and handing it back.

While you have control, every tool call to that desktop's MCP server is refused (so
the agent neither fights you for the mouse nor screenshots what you type). Handing
back wakes the agent: a waiting `ask_user_to_take_over` call returns with your note,
or, if the agent was blocked mid-task, a turn is queued in that conversation.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import db
from ..models import AgentDef, Role, Thread
from ..runtime import desktop_servers

router = APIRouter(prefix="/api/agents/{agent_id}/desktop", tags=["desktop"])


class ControlIn(BaseModel):
    thread_id: str | None = None  # the conversation you are looking at
    note: str = ""


def _agent(agent_id: str) -> AgentDef:
    with db.session() as s:
        a = s.get(AgentDef, agent_id)
    if not a:
        raise HTTPException(404, "agent not found")
    return a


def _servers(agent: AgentDef):
    servers = desktop_servers(agent)
    if not servers:
        raise HTTPException(409, f"{agent.name} has no desktop tool server. On the Tools page, "
                                 "tick 'controls an agent desktop' on its desktop server.")
    return servers


def _thread_ok(thread_id: str | None) -> str | None:
    if not thread_id:
        return None
    with db.session() as s:
        return thread_id if s.get(Thread, thread_id) else None


@router.get("")
async def desktop_state(agent_id: str, request: Request):
    agent = _agent(agent_id)
    broker = request.app.state.broker
    servers = desktop_servers(agent)
    states = [{"server_id": srv.id, "server": srv.name, **await broker.desktop_state(srv.id)}
              for srv in servers]
    return {"agent_id": agent.id, "desktop_url": agent.desktop_url, "has_desktop_tools": bool(servers),
            "user_has_control": any(s["control"] for s in states),
            "request": next((s["request"] for s in states if s["request"]), None),
            "servers": states}


@router.post("/take")
async def take_control(agent_id: str, body: ControlIn, request: Request):
    agent = _agent(agent_id)
    rt = request.app.state.runtime
    pending = None
    for srv in _servers(agent):
        await rt.broker.take_control(srv.id, {"thread_id": body.thread_id})
        pending = pending or (await rt.broker.desktop_state(srv.id))["request"]
    msg = f"You took control of {agent.name}'s desktop. Its desktop tools are paused."
    for tid in {_thread_ok(body.thread_id), pending and pending.get("thread_id")} - {None}:
        await rt.save_message(tid, Role.event, msg, kind="desktop_control", agent_id=agent.id)
    return await desktop_state(agent_id, request)


@router.post("/release")
async def release_control(agent_id: str, body: ControlIn, request: Request):
    agent = _agent(agent_id)
    rt = request.app.state.runtime
    note = body.note.strip()
    woke = []
    for srv in _servers(agent):
        before = await rt.broker.release_control(srv.id, note)
        req = before["request"]
        msg = f"You handed {agent.name}'s desktop back" + (f": {note}" if note else ".")
        if req:  # an agent is waiting in ask_user_to_take_over; the push above wakes it
            await rt.save_message(req["thread_id"], Role.event, msg, kind="desktop_release",
                                  agent_id=agent.id)
            woke.append({"waiting_agent": req["agent"], "thread_id": req["thread_id"]})
        elif before["blocked"]:
            # The agent hit the paused desktop mid-task and gave up; wake its conversation.
            tid = _thread_ok(before["blocked"].get("thread_id")) or _thread_ok(body.thread_id)
            if tid:
                prompt = (f"[Desktop handed back] The user has finished on {agent.name}'s desktop"
                          + (f" and says: {note}" if note else ".")
                          + " If work was waiting on that desktop, continue it now; delegate "
                            "again if needed. Otherwise acknowledge briefly.")
                job = await rt.enqueue_turn(tid, prompt, from_user=False, event_text=msg,
                                            kind="desktop_release")
                woke.append({"queued_turn": job.id, "thread_id": tid})
        elif _thread_ok(body.thread_id):
            await rt.save_message(body.thread_id, Role.event, msg, kind="desktop_release",
                                  agent_id=agent.id)
    return {**await desktop_state(agent_id, request), "woke": woke}
