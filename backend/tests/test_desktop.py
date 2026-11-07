"""Desktop handoff: you take control, the agent's desktop tools pause, you hand back,
the agent resumes. Uses a real stdio MCP server flagged as a desktop."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo

from app.config import settings

from .conftest import SCRIPTS, _tool_returns

DEMO_MCP = Path(__file__).parent / "demo_mcp_server.py"


def _all_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)]


def desk_user(messages, info: AgentInfo) -> ModelResponse:
    """Uses the desktop once, reports what the tool said."""
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("demo_add", {"a": 2, "b": 3})])
    return ModelResponse(parts=[TextPart(f"tool said {returns[0].content}")])


def asker(messages, info: AgentInfo) -> ModelResponse:
    """Asks you to take over, then uses the desktop, then reports."""
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("ask_user_to_take_over", {"reason": "Log in to Zabbix"})])
    last = returns[0]  # _tool_returns lists newest first
    if last.tool_name == "ask_user_to_take_over":
        if last.content["status"] != "handed_back":
            return ModelResponse(parts=[TextPart(f"Gave up: {last.content['status']}")])
        return ModelResponse(parts=[ToolCallPart("demo_add", {"a": 2, "b": 3})])
    note = next(r.content["note"] for r in _all_returns(messages) if r.tool_name == "ask_user_to_take_over")
    return ModelResponse(parts=[TextPart(f"Done after handoff ({note}); tool said {last.content}")])


SCRIPTS.update({"desk-user": desk_user, "asker": asker})


async def _desk_agent(hq, model: str):
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "demo", "transport": "stdio", "command": sys.executable,
        "args": [str(DEMO_MCP)], "desktop": True})).json()
    a = await hq.agent(name="Operator", model=model, tool_server_ids=[srv["id"]],
                       desktop_url="http://desk:3000")
    t = await hq.thread(a["id"])
    return a, t


async def test_tools_pause_while_you_have_control_and_resume_on_handback(hq):
    await hq.start_worker()
    a, t = await _desk_agent(hq, "fake:desk-user")
    r = await hq.http.post(f"/api/agents/{a['id']}/desktop/take", json={"thread_id": t["id"]})
    assert r.json()["user_has_control"] is True

    await hq.say(t["id"], "check the dashboard")
    [paused] = await hq.wait_agent_replies(t["id"], 1)
    assert paused.startswith("tool said The user has taken control")
    assert (await hq.http.get(f"/api/agents/{a['id']}/desktop")).json()["servers"][0]["blocked"]

    r = (await hq.http.post(f"/api/agents/{a['id']}/desktop/release",
                            json={"thread_id": t["id"], "note": "logged in"})).json()
    assert r["user_has_control"] is False and r["woke"][0]["thread_id"] == t["id"]
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies[1] == "tool said 5"  # the agent was woken and the desktop worked again


async def test_agent_asks_you_to_take_over_then_continues(hq):
    await hq.start_worker(concurrency=1)  # the waiting agent must give up its slot
    a, t = await _desk_agent(hq, "fake:asker")
    await hq.say(t["id"], "pull the Zabbix problems list")

    for _ in range(200):
        state = (await hq.http.get(f"/api/agents/{a['id']}/desktop")).json()
        if state["request"]:
            break
        await asyncio.sleep(0.05)
    assert state["request"]["reason"] == "Log in to Zabbix" and state["request"]["thread_id"] == t["id"]

    await hq.http.post(f"/api/agents/{a['id']}/desktop/take", json={"thread_id": t["id"]})
    await asyncio.sleep(0.2)
    r = (await hq.http.post(f"/api/agents/{a['id']}/desktop/release",
                            json={"thread_id": t["id"], "note": "logged in, 2FA done"})).json()
    assert r["woke"] == [{"waiting_agent": "Operator", "thread_id": t["id"]}]

    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply == "Done after handoff (logged in, 2FA done); tool said 5"
    kinds = [m.meta.get("kind") for m in hq.messages(t["id"]) if m.role.value == "event"]
    assert kinds[:3] == ["desktop_request", "desktop_control", "desktop_release"]


async def test_handoff_times_out_if_nobody_comes(hq, monkeypatch):
    monkeypatch.setattr(settings, "handoff_timeout", 1)
    await hq.start_worker()
    _, t = await _desk_agent(hq, "fake:asker")
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Gave up: no_response"]


async def test_take_requires_a_desktop_tool_server(hq):
    a = await hq.agent(name="No desk", model="fake:worker")
    r = await hq.http.post(f"/api/agents/{a['id']}/desktop/take", json={})
    assert r.status_code == 409
