"""The learning loop: feedback, corrections, reflections and what they produce."""

from __future__ import annotations

import asyncio
import time

from sqlmodel import select

from app import db, learning
from app.models import Feedback, Memory, Reflection, SkillProposal, UsageRecord

from .conftest import REFLECTED

FAST = {"model": "fake:reflector", "wait_minutes": 0, "feedback_wait_minutes": 0, "min_tool_calls": 3}


async def _setup(hq, model="fake:busy", **settings):
    REFLECTED.clear()
    r = await hq.http.put("/api/learning/settings", json={**FAST, **settings})
    assert r.status_code == 200, r.text
    await hq.start_worker()
    a = await hq.agent(name="Duke", model=model)
    t = await hq.thread(a["id"])
    return a, t


def _reflections() -> list[Reflection]:
    with db.session() as s:
        return list(s.exec(select(Reflection).order_by(Reflection.created_at)).all())


async def _until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = cond()
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def test_correction_detection():
    for text in ["No, use the TrueNAS datastore", "that's wrong, it's rewind", "Actually it runs at 2am",
                 "You should have checked the verify job too", "next time, put failures first",
                 "I told you the host is hades", "From now on use tables"]:
        assert learning.looks_like_correction(text), text
    for text in ["now check the logs", "thanks!", "great work", "what about the NAS?",
                 "can you also check the UPS", ""]:
        assert not learning.looks_like_correction(text), text


async def test_multi_step_work_is_reflected_on_and_becomes_a_skill_proposal(hq):
    a, t = await _setup(hq)
    await hq.say(t["id"], "what skills do we have?")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("Done after 3 looks")
    [r] = await _until(_reflections)
    assert r.status == "waiting" and r.reasons == ["3 tool calls"] and r.agent_name == "Duke"
    job = hq.jobs()[0]
    assert job.meta["tool_calls"] == 3 and job.meta["trace"][0]["tool"] == "list_skills"
    [done] = await learning.run_due(hq.runtime)
    assert done.status == "done" and done.summary.startswith("I looked back") and done.model == "fake:reflector"
    # The reflection saw the tool calls and the conversation.
    assert "list_skills" in REFLECTED[0] and "what skills do we have?" in REFLECTED[0]
    [p] = (await hq.http.get("/api/skill-proposals?status=pending")).json()
    assert p["name"] == "survey_skills" and p["source"] == "reflection" and done.proposal_ids == [p["id"]]
    assert any("from looking back" in m.content for m in hq.messages(t["id"]))
    log = (await hq.http.get("/api/learning/reflections")).json()
    assert log["reflections"][0]["proposals"][0]["name"] == "survey_skills" and log["ran_today"] == 1
    with db.session() as s:
        assert len(s.exec(select(UsageRecord).where(UsageRecord.model == "fake:reflector")).all()) == 1
    # Nothing waits any more.
    assert await learning.run_due(hq.runtime) == []


async def test_thumbs_down_with_a_note_becomes_a_lesson(hq):
    a, t = await _setup(hq, model="fake:echo")
    await hq.say(t["id"], "check the backups")
    await hq.wait_agent_replies(t["id"], 1)
    assert _reflections() == []  # a simple turn isn't worth a look by itself
    reply = next(m for m in hq.messages(t["id"]) if m.role.value == "agent")
    assert reply.meta["job_id"] == hq.jobs()[0].id
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": "down"})
    r = await hq.http.post(f"/api/messages/{reply.id}/feedback",
                           json={"rating": "down", "comment": "failures first, as a table"})
    assert r.json()["feedback"] == {"rating": "down", "comment": "failures first, as a table"}
    [ref] = _reflections()
    assert ref.reasons == ["you rated it 👎: “failures first, as a table”"]
    await learning.run_due(hq.runtime)
    with db.session() as s:
        [m] = s.exec(select(Memory).where(Memory.source == "reflection")).all()
    assert m.text == "Mike wants: failures first, as a table" and m.kind.value == "preference"
    assert any(x.meta.get("kind") == "lesson" and "noted for next time" in x.content
               for x in hq.messages(t["id"]))
    assert "[user rated this 👎: failures first, as a table]" in REFLECTED[0]
    # Clearing the rating removes it.
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": None})
    with db.session() as s:
        assert s.exec(select(Feedback)).all() == []
    assert next(x for x in hq.messages(t["id"]) if x.id == reply.id).meta.get("feedback") is None


async def test_a_correction_in_the_conversation_gets_the_last_job_a_look(hq):
    a, t = await _setup(hq, model="fake:echo", wait_minutes=60)
    await hq.say(t["id"], "check the backups on the NAS")
    await hq.wait_agent_replies(t["id"], 1)
    first_job = hq.jobs()[0].id
    await hq.say(t["id"], "No, use the TrueNAS datastore on rewind")
    await hq.wait_agent_replies(t["id"], 2)
    [r] = _reflections()
    assert r.job_id == first_job and r.reasons[0].startswith("you corrected it: “No, use the TrueNAS")
    assert await learning.run_due(hq.runtime) == []  # waits for the quiet time
    with db.session() as s:
        row = s.get(Reflection, r.id)
        row.due_at = learning._now()
        s.add(row)
        s.commit()
    [done] = await learning.run_due(hq.runtime)
    # The follow-up is in what the agent looked back at.
    assert "No, use the TrueNAS datastore on rewind" in REFLECTED[0] and done.memory_ids


async def test_no_reflection_when_learning_is_off_and_the_daily_cap_holds(hq):
    a, t = await _setup(hq)
    await hq.http.put(f"/api/agents/{a['id']}", json={**a, "learning_mode": "off"})
    await hq.say(t["id"], "skills?")
    await hq.wait_agent_replies(t["id"], 1)
    await asyncio.sleep(0.2)
    assert _reflections() == []
    await hq.http.put(f"/api/agents/{a['id']}", json={**a, "learning_mode": "propose"})
    await hq.http.put("/api/learning/settings", json={"max_per_day": 1})
    for i in range(2):
        await hq.say(t["id"], f"skills again {i}?")
        await hq.wait_agent_replies(t["id"], 2 + i)
    await _until(lambda: len(_reflections()) == 2)
    got = await learning.run_due(hq.runtime)
    assert [r.status for r in got] == ["done", "skipped"] and "daily limit" in got[1].summary
    # "Run now" ignores the cap.
    r = await hq.http.post(f"/api/learning/reflections/{got[1].id}/run")
    assert r.status_code == 202
    await _until(lambda: [x for x in _reflections() if x.id == got[1].id and x.status == "done"])


async def test_reflection_off_and_thumbs_up_on_skill_work_counts(hq):
    k = (await hq.http.post("/api/skills", json={
        "name": "check_pve_backups", "title": "Check Proxmox backups",
        "when_to_use": "When asked whether last night's Proxmox backups worked.",
        "body": "1. Open PBS on rewind.\n2. Check the last job.\n3. Report failures first."})).json()
    a, t = await _setup(hq, model="fake:skilled", reflect="off")
    await hq.say(t["id"], "backups?")
    await hq.wait_agent_replies(t["id"], 1)
    reply = next(m for m in hq.messages(t["id"]) if m.role.value == "agent")
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": "up"})
    assert _reflections() == []  # reflection is off
    stats = (await hq.http.get(f"/api/skills/{k['id']}")).json()["stats"]
    assert stats["up"] == 1 and stats["down"] == 0
    await hq.http.put("/api/learning/settings", json={"reflect": "signals"})
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": "up", "comment": "spot on"})
    [r] = _reflections()
    assert r.reasons == ["you rated it 👍: “spot on”"]  # it used a skill, so a 👍 is worth a look
    assert (await hq.http.put("/api/learning/settings", json={"reflect": "sometimes"})).status_code == 422
    # Only agent replies can be rated.
    user_msg = next(m for m in hq.messages(t["id"]) if m.role.value == "user")
    assert (await hq.http.post(f"/api/messages/{user_msg.id}/feedback", json={"rating": "up"})).status_code == 422
