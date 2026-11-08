from __future__ import annotations

import asyncio
import time

from sqlmodel import select

from app import db, slots, subagents
from app.models import AgentDef, JobStatus, Role, SubagentRequest, UsageRecord
from app.runtime import RunDeps

LIMITS = {"concurrent": 2, "per_task": 2, "per_day": 5, "max_minutes": 5, "max_requests": 10,
          "models": [{"provider_id": None, "model": "fake:worker"}], "tool_server_ids": None}


async def _spawner(hq, model="fake:spawner", mode="allowed", limits=None, **kw):
    a = await hq.agent(name="Scarlett", description="Research analyst", model=model,
                       subagent_mode=mode, subagent_limits=limits or LIMITS, **kw)
    return a, await hq.thread(a["id"])


def _subs() -> list[AgentDef]:
    with db.session() as s:
        return list(s.exec(select(AgentDef).where(AgentDef.is_subagent == True)).all())  # noqa: E712


async def _wait(cond, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_off_by_default_means_no_sub_agent_tools(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:spawner")
    assert a["subagent_mode"] == "off"
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "read the feeds")
    assert await hq.wait_agent_replies(t["id"], 1) == ["I can't create sub-agents."]
    assert _subs() == []


async def test_allowed_agent_spawns_a_sub_agent_that_reports_back_and_retires(hq):
    await hq.start_worker()
    a, t = await _spawner(hq)
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("done: Worker finished")
    [sub] = _subs()
    assert sub.parent_agent_id == a["id"] and sub.retired and not sub.enabled
    assert sub.model == "fake:worker" and sub.name.startswith("Feed reader (Scarlett #")
    assert sub.subagent_mode == "off"  # never nested
    # The exchange shows in the chat as a sub-agent crumb.
    [crumb] = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "delegation_result"]
    assert crumb.meta["subagent"] and crumb.meta["subagent_role"] == "Feed reader"
    assert crumb.meta["status"] == "done" and crumb.meta["requester"] == "Scarlett"
    # Hidden from your agent list and the export.
    assert [x["name"] for x in (await hq.http.get("/api/agents")).json()] == ["Scarlett"]
    assert len((await hq.http.get("/api/agents?include_subagents=true")).json()) == 2
    assert [x["name"] for x in (await hq.http.get("/api/agents/export")).json()["agents"]] == ["Scarlett"]
    # Both runs are on the ledger, the sub-agent's under its parent.
    with db.session() as s:
        rows = s.exec(select(UsageRecord)).all()
    assert {(r.agent_name, r.is_subagent) for r in rows} == {("Scarlett", False), (sub.name, True)}
    assert all(r.ok and r.requests >= 1 for r in rows)
    usage = (await hq.http.get("/api/usage")).json()
    assert usage["by_agent"][0]["agent"] == "Scarlett" and usage["by_agent"][0]["runs"] == 2
    assert usage["by_agent"][0]["subagent_runs"] == 1 and usage["subagents"]["runs"] == 1


async def test_per_task_limit_refuses_extra_sub_agents(hq):
    await hq.start_worker()
    _, t = await _spawner(hq, model="fake:spawner-2", limits={**LIMITS, "per_task": 1})
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    first, second = reply.split(" | ")
    assert first.startswith("done:")
    assert second.startswith("refused:") and "for this task (limit 1)" in second
    assert len(_subs()) == 1


async def test_team_cap_applies_across_agents(hq):
    await hq.start_worker()
    await hq.http.put("/api/settings/subagents", json={"max_per_day": 1})
    _, t = await _spawner(hq, model="fake:spawner-2")
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "team has used today's 1 sub-agents" in reply.split(" | ")[1]


async def test_refuses_sub_agent_named_after_an_existing_agent(hq):
    await hq.start_worker()
    await hq.agent(name="Alpha", description="Reads feeds", model="fake:worker")
    _, t = await _spawner(hq, model="fake:spawn-alpha")
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("refused:") and "would duplicate Alpha" in reply
    assert "isn't on your team" in reply  # Scarlett can't delegate to Alpha
    assert _subs() == []


async def test_refuses_sub_agent_that_duplicates_an_agent_by_description(hq, monkeypatch):
    await hq.start_worker()
    alpha = await hq.agent(name="Alpha", description="Reads RSS feeds and reports headlines",
                           model="fake:worker")
    _, t = await _spawner(hq, delegates=[alpha["id"]])

    async def fake_embed(texts, is_query):  # feeds-ish text points one way, everything else another
        return [[1.0, 0.0] if "feed" in x.lower() else [0.0, 1.0] for x in texts]

    monkeypatch.setattr(subagents, "_embed", fake_embed)
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "would duplicate Alpha" in reply and "Delegate this to Alpha instead." in reply


async def test_sub_agent_request_cap_stops_runaway_model_use(hq):
    await hq.start_worker()
    _, t = await _spawner(hq, limits={**LIMITS, "max_requests": 1,
                                      "models": [{"provider_id": None, "model": "fake:retrying"}]})
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("failed:") and "request_limit" in reply
    with db.session() as s:
        sub_rows = s.exec(select(UsageRecord).where(UsageRecord.is_subagent == True)).all()  # noqa: E712
    assert [r.ok for r in sub_rows] == [False]


async def test_sub_agent_time_limit(hq):
    sub = AgentDef(name="Slow (test)", model="fake:slow-worker", is_subagent=True,
                   subagent_limits={"max_minutes": 0.02, "max_requests": 5})
    with db.session() as s:
        s.add(sub)
        s.commit()
        s.refresh(sub)
    owner = await hq.agent(name="Owner", model="fake:worker")
    t = await hq.thread(owner["id"])
    started = time.monotonic()
    try:
        await hq.runtime.run_agent(sub, "slow job", RunDeps(thread_id=t["id"], agent_id=sub.id),
                                   as_worker=True)
        raise AssertionError("should have been stopped")
    except RuntimeError as e:
        assert "minute limit" in str(e)
    assert time.monotonic() - started < 5


async def test_ask_mode_request_approval_and_grant(hq, monkeypatch):
    await hq.start_worker()
    sent = []

    async def fake_notify(event):
        sent.append(event)
        return True

    monkeypatch.setattr(subagents, "notify", fake_notify)
    a, t = await _spawner(hq, model="fake:requester", mode="ask")
    await hq.say(t["id"], "prepare the morning brief")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Request: pending"]
    assert _subs() == []
    [req] = (await hq.http.get("/api/subagent-requests?status=pending")).json()
    assert req["agent"] == "Scarlett" and req["subagents"][0]["role"] == "Feed reader"
    assert req["estimate"]["total_minutes"] == 5 and req["estimate"]["subagents"][0]["model"] == "fake:worker"
    assert (await hq.http.get("/api/subagent-requests/pending-count")).json() == {"pending": 1}
    card = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "subagent_request"]
    assert card and card[0].meta["request_id"] == req["id"]
    assert sent[0]["type"] == "subagent_request" and sent[0]["agent"] == "Scarlett"

    r = await hq.http.post(f"/api/subagent-requests/{req['id']}/decide",
                           json={"approve": True, "note": "go ahead", "max_minutes": 3})
    assert r.status_code == 200, r.text
    grant = r.json()["grant"]
    assert (grant["count"], grant["max_minutes"]) == (1, 3)
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies[1].startswith("done: Worker finished")
    [sub] = _subs()
    assert sub.subagent_limits["max_minutes"] == 3
    with db.session() as s:
        assert s.get(SubagentRequest, req["id"]).used == 1
    assert sent[-1]["type"] == "subagent_decision" and sent[-1]["status"] == "approved"
    events = [m.content for m in hq.messages(t["id"]) if m.role == Role.event]
    assert "You approved Scarlett's sub-agent request: go ahead" in events
    # Deciding twice is refused.
    again = await hq.http.post(f"/api/subagent-requests/{req['id']}/decide", json={"approve": False})
    assert again.status_code == 409


async def test_ask_mode_cannot_spawn_without_approval_and_denial_wakes_agent(hq, monkeypatch):
    await hq.start_worker()
    monkeypatch.setattr(subagents, "notify", lambda e: asyncio.sleep(0, False))
    _, t = await _spawner(hq, model="fake:spawner", mode="ask")
    await hq.say(t["id"], "read the feeds")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "need the user's approval first" in reply

    a2 = await hq.agent(name="Duke", model="fake:requester", subagent_mode="ask")
    t2 = await hq.thread(a2["id"])
    await hq.say(t2["id"], "brief me")
    await hq.wait_agent_replies(t2["id"], 1)
    [req] = (await hq.http.get("/api/subagent-requests?status=pending")).json()
    r = await hq.http.post(f"/api/subagent-requests/{req['id']}/decide",
                           json={"approve": False, "note": "use Scarlett"})
    assert r.json()["status"] == "denied"
    replies = await hq.wait_agent_replies(t2["id"], 2)
    assert replies[1] == "Understood, I'll do it myself."
    assert _subs() == []


async def test_pending_request_limit(hq, monkeypatch):
    await hq.start_worker()
    monkeypatch.setattr(subagents, "notify", lambda e: asyncio.sleep(0, False))
    await hq.http.put("/api/settings/subagents", json={"max_pending_per_agent": 1})
    _, t = await _spawner(hq, model="fake:requester", mode="ask")
    await hq.say(t["id"], "one")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "two")
    replies = await hq.wait_agent_replies(t["id"], 2)
    assert replies == ["Request: pending", "Request: refused"]


async def test_limits_are_validated(hq):
    r = await hq.http.post("/api/agents", json={"name": "X", "model": "fake:worker",
                                                "subagent_limits": {**LIMITS, "concurrent": 99}})
    assert r.status_code == 422 and "concurrent" in r.text
    # Tool servers for sub-agents are narrowed to the agent's own.
    r = await hq.http.post("/api/agents", json={"name": "Y", "model": "fake:worker",
                                                "subagent_limits": {**LIMITS, "tool_server_ids": ["nope"]}})
    assert r.json()["subagent_limits"]["tool_server_ids"] == []


async def test_provider_slots_limit_concurrent_requests(hq):
    slots.use(hq.broker.r)
    active, peak = 0, 0

    async def job():
        nonlocal active, peak
        async with slots.slot("p1", 2):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.3)
            active -= 1

    await asyncio.gather(*(job() for _ in range(5)))
    assert peak == 2
    assert await slots.in_use("p1") == 0


async def test_provider_with_slots_wraps_its_models(hq):
    from app.llm import build_model_for

    p = (await hq.http.post("/api/providers", json={"name": "GPU box", "kind": "ollama",
                                                   "base_url": "http://gpu:11434",
                                                   "max_concurrent": 1})).json()
    assert p["max_concurrent"] == 1
    m = build_model_for(p["id"], "qwen3:14b", "test")
    assert isinstance(m, slots.SlotLimitedModel) and m.limit == 1
    # Updating without the field keeps it.
    p = (await hq.http.put(f"/api/providers/{p['id']}", json={"name": "GPU box", "kind": "ollama",
                                                             "base_url": "http://gpu:11434"})).json()
    assert p["max_concurrent"] == 1


def test_old_databases_get_new_columns(tmp_path, monkeypatch):
    import sqlite3

    from app.config import reload_settings

    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AHQ_DB_URL", "")
    c = sqlite3.connect(tmp_path / "agent_hq.db")
    c.execute("create table modelprovider (id varchar primary key, name varchar, kind varchar, "
              "base_url varchar, api_key varchar, oauth json, enabled boolean, created_at datetime)")
    c.execute("insert into modelprovider values ('p1', 'Einstein', 'ollama', null, null, null, 1, null)")
    c.commit()
    c.close()
    reload_settings()
    db.reset_engine()
    db.init_db()
    from app.models import ModelProvider

    with db.session() as s:
        assert s.get(ModelProvider, "p1").max_concurrent == 0
