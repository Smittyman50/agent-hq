from __future__ import annotations

import json
import sys
from pathlib import Path

from app.models import JobKind, JobStatus, Role

DEMO_MCP = Path(__file__).parent / "demo_mcp_server.py"


async def _team(hq, boss="fake:boss", worker="fake:worker", worker_name="Alpha", worker_pool="default"):
    w = await hq.agent(name=worker_name, description="Does research", model=worker, pool=worker_pool)
    b = await hq.agent(name="Lead", description="Supervisor", model=boss, delegates=[w["id"]])
    t = await hq.thread(b["id"])
    return b, w, t


async def test_wait_delegation_relays_result_through_workers(hq):
    await hq.start_worker()
    _, w, t = await _team(hq)
    await hq.say(t["id"], "check the RSS feeds")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished"]
    turn, task = hq.jobs()
    assert (turn.kind, task.kind) == (JobKind.turn, JobKind.task)
    assert task.assignee_agent_id == w["id"] and task.origin_thread_id == t["id"]
    assert task.status == JobStatus.done and task.worker == "w1"
    assert [m.role for m in hq.messages(t["id"])] == [Role.user, Role.event, Role.agent]


async def test_single_slot_worker_does_not_deadlock(hq):
    # The supervisor waits on its delegate; it must give up its slot while waiting.
    await hq.start_worker(concurrency=1)
    _, _, t = await _team(hq)
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished"]


async def test_agent_pinned_to_pool_runs_on_that_pool(hq):
    await hq.start_worker(name="general", pools=["default"])
    await hq.start_worker(name="desktop-host", pools=["desktop"])
    _, _, t = await _team(hq, worker_pool="desktop")
    await hq.say(t["id"], "look at the dashboard")
    await hq.wait_agent_replies(t["id"], 1)
    turn, task = hq.jobs()
    assert turn.worker == "general" and task.worker == "desktop-host" and task.pool == "desktop"


async def test_renamed_agent_needs_no_code_change(hq):
    await hq.start_worker()
    _, w, t = await _team(hq, worker_name="Original")
    w["name"] = "Renamed Scout"
    assert (await hq.http.put(f"/api/agents/{w['id']}", json=w)).status_code == 200
    await hq.say(t["id"], "go")
    await hq.wait_agent_replies(t["id"], 1)
    assert hq.jobs()[1].assignee_agent_id == w["id"]


async def test_background_job_reports_back_in_origin_thread(hq):
    await hq.start_worker()
    _, _, t = await _team(hq, boss="fake:boss-bg")
    await hq.say(t["id"], "long research task")
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies[0].startswith("Report:") and "background" in replies[0]
    assert replies[1] == "Update for you: Worker finished"


async def test_failed_worker_is_still_reported(hq):
    await hq.start_worker()
    _, _, t = await _team(hq, worker="fake:broken")
    await hq.say(t["id"], "do it")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("Report: Alpha hit an error")
    assert hq.jobs()[1].status == JobStatus.failed


async def test_job_survives_worker_crash(hq):
    """Worker A dies mid-task; worker B reclaims the unacked job and finishes it."""
    a = await hq.start_worker(name="A", pools=["scouts"], claim_idle_ms=60000)
    await hq.start_worker(name="main", pools=["default"])
    _, _, t = await _team(hq, worker="fake:hang-once", worker_pool="scouts")
    await hq.say(t["id"], "survive this")

    import asyncio
    for _ in range(100):  # wait until A has picked the task up
        jobs = hq.jobs()
        if len(jobs) == 2 and jobs[1].status == JobStatus.running:
            break
        await asyncio.sleep(0.05)
    await a.stop(graceful=False)  # crash: no ack
    await hq.start_worker(name="B", pools=["scouts"], claim_idle_ms=300)

    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished"]
    task = hq.jobs()[1]
    assert task.worker == "B" and task.attempts == 2 and task.status == JobStatus.done


async def test_agent_without_delegates_has_no_team_and_keeps_history(hq):
    await hq.start_worker()
    b = await hq.agent(name="Solo", model="fake:boss")
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "hi")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "again")
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies[0].startswith("I have no team. (turn 1")
    assert replies[1].startswith("I have no team. (turn 2")  # history came from the checkpointer


async def test_worker_uses_registered_mcp_tool(hq):
    await hq.start_worker()
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "demo", "transport": "stdio", "command": sys.executable,
        "args": [str(DEMO_MCP)]})).json()
    probe = await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")
    assert probe.status_code == 200 and probe.json()[0]["name"] == "add"
    await hq.agent(name="Calc", description="Adds", model="fake:worker", tool_server_ids=[srv["id"]])
    b = await hq.agent(name="Boss", model="fake:boss", delegates=["*"])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "add numbers")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished (tool said 5)"]


async def test_workers_are_listed(hq):
    await hq.start_worker(name="gpu-box", pools=["gpu", "default"])
    import asyncio
    await asyncio.sleep(0.3)
    workers = (await hq.http.get("/api/system/workers")).json()
    assert workers[0]["name"] == "gpu-box" and workers[0]["pools"] == ["gpu", "default"]


async def test_export_import_roundtrip(hq):
    w = await hq.agent(name="W", description="worker", model="fake:worker", pool="gpu")
    b = await hq.agent(name="B", model="fake:boss", delegates=[w["id"]], is_default=True)
    await hq.http.post("/api/subscriptions", json={
        "name": "alerts", "topic_filter": "homelab/alerts/#", "agent_id": b["id"]})
    exported = (await hq.http.get("/api/agents/export")).json()
    agents = {a["name"]: a for a in exported["agents"]}
    assert agents["B"]["delegates"] == ["W"] and agents["W"]["pool"] == "gpu"
    assert exported["subscriptions"][0]["agent"] == "B"
    r = (await hq.http.post("/api/agents/import", json=exported)).json()
    assert r == {"providers": 0, "agents": 2, "tool_servers": 0, "subscriptions": 1}


async def test_example_crew_imports_as_data(hq):
    data = json.loads((Path(__file__).parents[2] / "examples" / "agents.example.json").read_text())
    await hq.http.post("/api/agents/import", json=data)
    agents = {a["name"]: a for a in (await hq.http.get("/api/agents")).json()}
    assert {agents[n]["id"] for n in ("Scarlett", "Snake Eyes")} == set(agents["Duke"]["delegates"])
    assert len(agents["Snake Eyes"]["tool_server_ids"]) == 1
