"""Automatic recreate: once a deploy has rebuilt the workbench image, each idle workbench
moves to it by itself (files kept), and a run that starts meanwhile waits for it."""

from __future__ import annotations

import asyncio

from app import db, resources
from app.models import Job, JobStatus, Resource

from .test_resources import FakeDocker, docker  # noqa: F401  (the fake Docker and its fixture)

WB = "agent-hq/workbench:latest"


async def workbench(hq):
    a = await hq.agent(name="Mainframe", description="Codes")
    res = (await hq.http.post("/api/resources", json={"kind": "workbench", "agent_ids": [a["id"]]})).json()
    return a, res


async def run_pass(hq, docker):
    d = resources.Docker(url="http://docker", transport=__import__("httpx").MockTransport(docker.handler))
    return await resources.auto_recreate_pass(hq.broker.r, d)


def job(thread_id: str, agent_id: str, status: JobStatus) -> str:
    with db.session() as s:
        j = Job(origin_thread_id=thread_id, assignee_agent_id=agent_id, task="work", status=status)
        s.add(j)
        s.commit()
        return j.id


def set_status(job_id: str, status: JobStatus) -> None:
    with db.session() as s:
        j = s.get(Job, job_id)
        j.status = status
        s.add(j)
        s.commit()


async def test_an_idle_workbench_moves_to_the_rebuilt_image(hq, docker):
    a, res = await workbench(hq)
    before = docker.containers["ahq-workbench-1"]
    assert await run_pass(hq, docker) == {}  # nothing rebuilt yet: left alone
    docker.rebuild(WB)
    assert await run_pass(hq, docker) == {"workbench-1": "recreated"}
    after = docker.containers["ahq-workbench-1"]
    assert after is not before and after["Image"] == docker.ids[WB]
    assert after["State"]["Status"] == "running"
    # Same data: the volume is mounted again, not replaced.
    assert {m["Source"] for m in after["body"]["HostConfig"]["Mounts"]} == \
        {m["Source"] for m in before["Mounts"] if m["Type"] == "volume"} | \
        {m["Source"] for m in before["Mounts"] if m["Type"] == "bind"}
    assert "ahq-workbench-1" in docker.volumes
    assert await run_pass(hq, docker) == {}  # current now
    assert not await hq.broker.r.exists(resources.updating_key(res["id"]))


async def test_a_busy_workbench_waits_until_its_agents_are_done(hq, docker):
    a, res = await workbench(hq)
    t = await hq.thread(a["id"])
    j = job(t["id"], a["id"], JobStatus.running)  # e.g. waiting on your approval
    docker.rebuild(WB)
    old = docker.containers["ahq-workbench-1"]
    assert await run_pass(hq, docker) == {"workbench-1": "busy"}
    assert docker.containers["ahq-workbench-1"] is old
    set_status(j, JobStatus.queued)
    assert await run_pass(hq, docker) == {"workbench-1": "busy"}  # queued counts too
    set_status(j, JobStatus.done)
    assert await run_pass(hq, docker) == {"workbench-1": "recreated"}


async def test_a_sub_agent_on_the_workbench_counts_as_busy(hq, docker):
    a, res = await workbench(hq)
    with db.session() as s:
        servers = [t.id for t in resources.servers_of(s, res["id"])]
    from app.models import AgentDef

    with db.session() as s:  # a sub-agent its owner made, given the owner's workbench
        sub = AgentDef(name="Helper", model="fake:worker", is_subagent=True, tool_server_ids=servers)
        s.add(sub)
        s.commit()
        sub_id = sub.id
    t = await hq.thread(a["id"])
    job(t["id"], sub_id, JobStatus.running)
    docker.rebuild(WB)
    assert await run_pass(hq, docker) == {"workbench-1": "busy"}


async def test_work_that_starts_while_flagging_still_wins(hq, docker, monkeypatch):
    # Idle at the first look, busy at the second (after the flag): leave it, clear the flag.
    a, res = await workbench(hq)
    looks = iter([[], ["Mainframe"]])
    monkeypatch.setattr(resources, "busy_agents", lambda s, r: next(looks))
    docker.rebuild(WB)
    old = docker.containers["ahq-workbench-1"]
    assert await run_pass(hq, docker) == {"workbench-1": "busy"}
    assert docker.containers["ahq-workbench-1"] is old
    assert not await hq.broker.r.exists(resources.updating_key(res["id"]))


async def test_stopped_desktop_and_unmanaged_ones_are_left_alone(hq, docker):
    a, res = await workbench(hq)
    await hq.http.post(f"/api/resources/{res['id']}/stop")
    desk = (await hq.http.post("/api/resources", json={"kind": "desktop", "agent_ids": [a["id"]]})).json()
    docker.rebuild(WB)
    docker.rebuild("agent-hq/desktop:latest")
    assert await run_pass(hq, docker) == {}
    assert docker.containers["ahq-workbench-1"]["State"]["Status"] == "exited"  # still stopped
    # Desktops are opt-in (AHQ_AUTO_RECREATE_KINDS), since recreating one closes its browser.
    monkeypatch_kinds = resources.settings.auto_recreate_kinds
    try:
        resources.settings.auto_recreate_kinds = "workbench,package,desktop"
        assert await run_pass(hq, docker) == {desk["name"]: "recreated"}
        with db.session() as s:  # docker compose runs it: not ours to recreate
            r = s.get(Resource, res["id"])
            r.managed = False
            s.add(r)
            s.commit()
        await hq.http.post(f"/api/resources/{res['id']}/start")
        assert await run_pass(hq, docker) == {}
    finally:
        resources.settings.auto_recreate_kinds = monkeypatch_kinds


async def test_a_run_waits_for_the_recreate_to_finish(hq):
    resources.use(hq.broker.r)
    await hq.broker.r.set(resources.updating_key("res1"), "1", ex=30)
    waiter = asyncio.create_task(resources.wait_while_updating("res1"))
    await asyncio.sleep(0.8)
    assert not waiter.done()  # held while the container is being replaced
    await hq.broker.r.delete(resources.updating_key("res1"))
    await asyncio.wait_for(waiter, 2)
    # Not being recreated, or not a resource at all: no wait.
    await asyncio.wait_for(resources.wait_while_updating("res2"), 0.5)
    await asyncio.wait_for(resources.wait_while_updating(None), 0.5)


async def test_a_turn_on_a_workbench_being_recreated_starts_once_it_is_back(hq):
    import sys
    from pathlib import Path

    from pydantic_ai import ModelResponse, TextPart

    from app.models import ToolServer

    from .conftest import SCRIPTS

    SCRIPTS["plain"] = lambda messages, info: ModelResponse(parts=[TextPart("done")])
    await hq.start_worker()
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "demo", "transport": "stdio", "command": sys.executable,
        "args": [str(Path(__file__).parent / "demo_mcp_server.py")]})).json()
    with db.session() as s:
        row = s.get(ToolServer, srv["id"])
        row.resource_id = "res-being-recreated"
        s.add(row)
        s.commit()
    a = await hq.agent(name="Coder", model="fake:plain", tool_server_ids=[srv["id"]])
    t = await hq.thread(a["id"])
    await hq.broker.r.set(resources.updating_key("res-being-recreated"), "1", ex=30)
    await hq.say(t["id"], "go")
    await asyncio.sleep(2)
    assert not [m for m in hq.messages(t["id"]) if m.role == "agent"]  # held, not failed
    await hq.broker.r.delete(resources.updating_key("res-being-recreated"))
    assert await hq.wait_agent_replies(t["id"], 1, timeout=20) == ["done"]
