"""Deleting a chat removes it from the sidebar and closes it, but keeps its history."""

from __future__ import annotations

from sqlmodel import select

from app import db, scheduler
from app.models import Job, Schedule, Thread

from .conftest import SCRIPTS, worker_fn

SCRIPTS["delete-worker"] = worker_fn


async def test_deleted_chat_leaves_the_list_and_takes_no_messages(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", description="Does research", model="fake:delete-worker")
    keep = await hq.thread(a["id"])
    gone = await hq.thread(a["id"])
    await hq.say(gone["id"], "hello")
    await hq.wait_agent_replies(gone["id"], 1)

    r = await hq.http.delete(f"/api/threads/{gone['id']}")
    assert r.status_code == 204, r.text

    ids = [t["id"] for t in (await hq.http.get("/api/threads")).json()]
    assert keep["id"] in ids and gone["id"] not in ids

    r = await hq.http.post(f"/api/threads/{gone['id']}/messages", json={"content": "again?"})
    assert r.status_code == 404
    assert (await hq.http.delete(f"/api/threads/{gone['id']}")).status_code == 404
    assert (await hq.http.delete("/api/threads/nope")).status_code == 404

    # History other pages rely on (jobs → usage, traces, learning) is kept.
    with db.session() as s:
        assert s.get(Thread, gone["id"]).deleted_at is not None
        assert any(j.origin_thread_id == gone["id"] for j in s.exec(select(Job)).all())


async def test_schedule_starts_a_new_chat_when_its_chat_was_deleted(hq):
    a = await hq.agent(name="Duke", description="Lead", model="fake:delete-worker")
    with db.session() as s:
        row = Schedule(agent_id=a["id"], name="Morning", task="brief me")
        s.add(row)
        s.commit()
        s.refresh(row)
    first = scheduler.ensure_thread(row)
    assert scheduler.ensure_thread(row) == first
    assert (await hq.http.delete(f"/api/threads/{first}")).status_code == 204
    assert scheduler.ensure_thread(row) != first
