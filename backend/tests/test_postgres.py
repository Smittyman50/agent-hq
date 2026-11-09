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
    hq.sign_in()
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


def test_upgrade_adds_new_columns_on_postgres(postgres_url, tmp_path, monkeypatch):
    """An existing Postgres database (from before sub-agents) gets the new columns,
    including the enum-typed one, without manual ALTER TABLEs."""
    import sqlalchemy as sa

    from app import db
    from app.config import reload_settings
    from app.models import AgentDef, ModelProvider

    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AHQ_DB_URL", postgres_url)
    reload_settings()
    db.reset_engine()
    db.init_db()
    eng = db.engine()
    with eng.begin() as c:
        c.execute(sa.text("DELETE FROM agentdef WHERE name = 'Old'"))
        for col in ("subagent_mode", "subagent_limits", "is_subagent", "parent_agent_id",
                    "spawned_by_job_id", "expires_at", "retired", "learning_mode"):
            c.execute(sa.text(f'ALTER TABLE agentdef DROP COLUMN IF EXISTS "{col}"'))
        c.execute(sa.text('ALTER TABLE modelprovider DROP COLUMN IF EXISTS "max_concurrent"'))
        c.execute(sa.text("DROP TYPE IF EXISTS subagentmode"))
        c.execute(sa.text("DROP TYPE IF EXISTS learningmode"))
        c.execute(sa.text("INSERT INTO agentdef (id, name, description, pronouns, instructions, model, "
                          "vision_model, vision_coords, vision_zoom, fallback_model, pool, tool_server_ids, "
                          "delegates, is_default, memory_enabled, avatar_version, avatar_animated, enabled, "
                          "created_at, updated_at) VALUES ('old1', 'Old', '', '', '', 'x', '', "
                          "'relative_1000', true, '', 'default', '[]', '[]', false, true, 0, false, true, "
                          "now(), now())"))
    db.reset_engine()
    db.init_db()
    with db.session() as s:
        a = s.get(AgentDef, "old1")
        assert a.subagent_mode == "off" and a.is_subagent is False and a.retired is False
        assert a.learning_mode == "propose"  # existing agents start proposing skills
        assert all(p.max_concurrent == 0 for p in s.exec(sa.select(ModelProvider)).scalars())
