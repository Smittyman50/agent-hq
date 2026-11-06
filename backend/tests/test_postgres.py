"""Same flows against real Postgres (database + LangGraph checkpointer), with two
workers that each open their own checkpointer connection, as separate machines would."""

from __future__ import annotations

import contextlib

import pytest

from app import db
from app.config import reload_settings
from app.runtime import open_checkpointer


@pytest.fixture
async def pg_hq(hq, postgres_url, monkeypatch):
    monkeypatch.setenv("AHQ_DB_URL", postgres_url)
    reload_settings()
    db.reset_engine()
    from sqlmodel import SQLModel
    SQLModel.metadata.drop_all(db.engine())
    db.init_db()
    async with contextlib.AsyncExitStack() as stack:
        hq.pg_savers = [await stack.enter_async_context(open_checkpointer()) for _ in range(2)]
        yield hq
        for w in hq.workers:
            await w.stop(graceful=False)
        hq.workers.clear()


async def test_delegation_and_history_on_postgres(pg_hq):
    hq = pg_hq
    await hq.start_worker(name="m1", pools=["default"], checkpointer=hq.pg_savers[0])
    await hq.start_worker(name="m2", pools=["gpu"], checkpointer=hq.pg_savers[1])
    w = await hq.agent(name="Scout", description="research", model="fake:worker", pool="gpu")
    b = await hq.agent(name="Chief", model="fake:boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "first")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Worker finished"]
    workers = {j.kind.value: j.worker for j in hq.jobs()}
    assert workers == {"turn": "m1", "task": "m2"}

    solo = await hq.agent(name="Solo", model="fake:boss")
    t2 = await hq.thread(solo["id"])
    await hq.say(t2["id"], "one")
    await hq.wait_agent_replies(t2["id"], 1)
    await hq.say(t2["id"], "two")
    replies = await hq.wait_agent_replies(t2["id"], 2)
    assert replies[1].startswith("I have no team. (turn 2")  # history persisted in Postgres
