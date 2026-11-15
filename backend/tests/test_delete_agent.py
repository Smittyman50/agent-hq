"""Deleting an agent for good: disable first, then delete; its own things go with it."""

from __future__ import annotations

from sqlmodel import select

from app import db
from app.models import (AgentDef, Job, Memory, MemoryScope, Message, Schedule, Subscription, Thread,
                        WorkItem, WorkStatus)

from .conftest import SCRIPTS, worker_fn

SCRIPTS["purge-worker"] = worker_fn


async def test_agent_must_be_disabled_before_it_can_be_deleted(hq):
    a = await hq.agent(name="Snake Eyes", description="Quiet", model="fake:purge-worker")
    plan = (await hq.http.get(f"/api/agents/{a['id']}/delete-plan")).json()
    assert plan["can_delete"] is False and "disable" in plan["reason"]
    r = await hq.http.delete(f"/api/agents/{a['id']}/permanent")
    assert r.status_code == 409, r.text
    assert (await hq.http.delete(f"/api/agents/{a['id']}")).status_code == 204
    assert (await hq.http.get(f"/api/agents/{a['id']}/delete-plan")).json()["can_delete"] is True
    assert (await hq.http.delete("/api/agents/nope/permanent")).status_code == 404


async def test_deleting_removes_what_it_owns_and_keeps_the_rest(hq):
    await hq.start_worker()
    boss = await hq.agent(name="Duke", description="Lead", model="fake:purge-worker")
    a = await hq.agent(name="Scarlett", description="Does research", model="fake:purge-worker")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    await hq.wait_agent_replies(t["id"], 1)
    with db.session() as s:
        d = s.get(AgentDef, boss["id"])
        d.delegates = [a["id"]]
        s.add(d)
        s.add(Schedule(agent_id=a["id"], name="Morning", task="brief me"))
        s.add(Subscription(name="alerts", topic_filter="lab/#", agent_id=a["id"]))
        s.add(Memory(text="mine", scope=MemoryScope.agent, agent_id=a["id"]))
        s.add(Memory(text="team", scope=MemoryScope.team, created_by=a["id"]))
        s.add(WorkItem(title="Fix backups", owner_agent_id=a["id"], status=WorkStatus.in_progress,
                       thread_id=t["id"]))
        s.commit()

    assert (await hq.http.delete(f"/api/agents/{a['id']}")).status_code == 204
    plan = (await hq.http.get(f"/api/agents/{a['id']}/delete-plan")).json()
    assert plan["conversations"] == 1 and plan["schedules"] == 1 and plan["goals_unassigned"] == 1
    r = await hq.http.delete(f"/api/agents/{a['id']}/permanent")
    assert r.status_code == 204, r.text
    assert (await hq.http.get(f"/api/agents/{a['id']}")).status_code == 404

    with db.session() as s:
        assert s.get(Thread, t["id"]) is None
        assert not s.exec(select(Message).where(Message.thread_id == t["id"])).all()
        assert not s.exec(select(Job).where(Job.origin_thread_id == t["id"])).all()
        assert not s.exec(select(Schedule).where(Schedule.agent_id == a["id"])).all()
        assert not s.exec(select(Subscription).where(Subscription.agent_id == a["id"])).all()
        texts = [m.text for m in s.exec(select(Memory)).all()]
        assert "mine" not in texts and "team" in texts
        g = s.exec(select(WorkItem).where(WorkItem.title == "Fix backups")).one()
        assert g.owner_agent_id is None and g.thread_id is None and g.status == WorkStatus.backlog
        assert s.get(AgentDef, boss["id"]).delegates == []
