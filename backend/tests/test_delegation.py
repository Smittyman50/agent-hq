from __future__ import annotations

import sys
from pathlib import Path

from sqlmodel import select

from app import db
from app.models import Job, Message, Role

from .conftest import make_agent

DEMO_MCP = Path(__file__).parent / "demo_mcp_server.py"


def _setup(client, boss_model="fake:boss", worker_model="fake:worker", worker_name="Alpha"):
    w = make_agent(client, name=worker_name, description="Does research", model=worker_model)
    b = make_agent(client, name="Lead", description="Supervisor", model=boss_model,
                   delegates=[w["id"]])
    t = client.post("/api/threads", json={"agent_id": b["id"]}).json()
    return b, w, t


def _messages(thread_id):
    with db.session() as s:
        return s.exec(select(Message).where(Message.thread_id == thread_id)
                      .order_by(Message.created_at)).all()


async def test_wait_delegation_relays_result_to_user(client, runtime):
    _, w, t = _setup(client)
    reply = await runtime.run_turn(t["id"], "check the RSS feeds")
    assert reply == "Report: Worker finished"
    with db.session() as s:
        job = s.exec(select(Job)).one()
    assert job.status == "done" and job.assignee_agent_id == w["id"]
    assert job.origin_thread_id == t["id"]
    roles = [m.role for m in _messages(t["id"])]
    assert roles == [Role.user, Role.event, Role.agent]


async def test_renamed_agent_needs_no_code_change(client, runtime):
    _, w, t = _setup(client, worker_name="Original")
    w["name"] = "Renamed Scout"
    assert client.put(f"/api/agents/{w['id']}", json=w).status_code == 200
    await runtime.run_turn(t["id"], "go")
    with db.session() as s:
        assert s.exec(select(Job)).one().assignee_agent_id == w["id"]


async def test_background_job_reports_back_in_origin_thread(client, runtime):
    _, _, t = _setup(client, boss_model="fake:boss-bg")
    first = await runtime.run_turn(t["id"], "long research task")
    assert first.startswith("Report:") and "background" in first
    await runtime.wait_for_background()
    agent_msgs = [m.content for m in _messages(t["id"]) if m.role == Role.agent]
    assert agent_msgs[-1] == "Update for you: Worker finished"


async def test_failed_worker_is_still_reported(client, runtime):
    _, _, t = _setup(client, worker_model="fake:broken")
    reply = await runtime.run_turn(t["id"], "do it")
    assert reply.startswith("Report: Alpha hit an error")
    with db.session() as s:
        assert s.exec(select(Job)).one().status == "failed"


async def test_agent_without_delegates_has_no_team(client, runtime):
    b = make_agent(client, name="Solo", model="fake:boss")
    t = client.post("/api/threads", json={"agent_id": b["id"]}).json()
    assert await runtime.run_turn(t["id"], "hi") == "I have no team."


async def test_worker_uses_registered_mcp_tool(client, runtime):
    srv = client.post("/api/tool-servers", json={
        "name": "demo", "transport": "stdio", "command": sys.executable,
        "args": [str(DEMO_MCP)]}).json()
    probe = client.get(f"/api/tool-servers/{srv['id']}/tools")
    assert probe.status_code == 200 and probe.json()[0]["name"] == "add"
    w = make_agent(client, name="Calc", description="Adds", model="fake:worker",
                   tool_server_ids=[srv["id"]])
    b = make_agent(client, name="Boss", model="fake:boss", delegates=["*"])
    t = client.post("/api/threads", json={"agent_id": b["id"]}).json()
    reply = await runtime.run_turn(t["id"], "add numbers")
    assert reply == "Report: Worker finished (tool said 5)"


def test_export_import_roundtrip(client):
    w = make_agent(client, name="W", description="worker", model="fake:worker")
    make_agent(client, name="B", model="fake:boss", delegates=[w["id"]], is_default=True)
    exported = client.get("/api/agents/export").json()
    assert {a["name"]: a["delegates"] for a in exported["agents"]}["B"] == ["W"]
    exported["agents"][0]["instructions"] = "changed"
    assert client.post("/api/agents/import", json=exported).json()["agents"] == 2


def test_example_crew_imports_as_data(client):
    import json
    data = json.loads((Path(__file__).parents[2] / "examples" / "agents.example.json").read_text())
    client.post("/api/agents/import", json=data)
    agents = {a["name"]: a for a in client.get("/api/agents").json()}
    assert {agents[n]["id"] for n in ("Scarlett", "Snake Eyes")} == set(agents["Duke"]["delegates"])
    assert len(agents["Snake Eyes"]["tool_server_ids"]) == 1
