"""Evals: test cases from replies and feedback, replayed tool calls, the judge, and testing
skill changes (with Auto-mode changes applied only if they don't make things worse)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlmodel import select

from app import db, evals
from app.models import Skill, SkillProposal
from app.runtime import load_agent

LAB = Path(__file__).with_name("lab_mcp_server.py")


@pytest.fixture
def lab(tmp_path):
    return tmp_path


async def _lab_agent(hq, lab, **server):
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "lab", "transport": "stdio", "command": sys.executable, "args": [str(LAB)],
        "env": {"LAB_DIR": str(lab)}, **server})).json()
    a = await hq.agent(name="Ops", model="fake:operator", tool_server_ids=[srv["id"]])
    return srv, a


async def _run(hq, case_ids):
    r = await hq.http.post("/api/evals/runs", json={"case_ids": case_ids})
    assert r.status_code == 202, r.text
    [done] = await evals.run_due(hq.runtime)
    return (await hq.http.get(f"/api/evals/runs/{done.id}")).json()


async def test_a_reply_becomes_a_test_that_replays_its_tool_calls(hq, lab):
    await hq.http.put("/api/evals/settings", json={"judge_model": "fake:judge"})
    await hq.start_worker()
    srv, a = await _lab_agent(hq, lab)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "restart the web VM")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "web restarted" in reply and (lab / "restarted-web").exists()
    (lab / "restarted-web").unlink()
    (lab / "listed").unlink()

    msg = next(m for m in hq.messages(t["id"]) if m.role.value == "agent")
    case = (await hq.http.post("/api/evals/cases/from-message", json={"message_id": msg.id})).json()
    assert case["status"] == "draft" and case["agent_id"] == a["id"] and case["prompt"] == "restart the web VM"
    assert case["context"] == ""  # the request itself isn't "earlier conversation"
    assert [s["tool"] for s in case["recording"]] == ["lab_list_vms", "lab_restart_vm"]
    assert case["recording"][1]["result"] == "web restarted" and not case["recording_partial"]
    # Keeping it needs a check.
    bad = await hq.http.put(f"/api/evals/cases/{case['id']}", json={**case, "status": "active"})
    assert bad.status_code == 422
    checks = [{"type": "contains", "value": "web restarted"}, {"type": "tool_called", "value": "restart_vm"},
              {"type": "rubric", "value": "Says the VM was restarted."}]
    case = (await hq.http.put(f"/api/evals/cases/{case['id']}", json={**case, "status": "active", "checks": checks})).json()

    run = await _run(hq, [case["id"]])
    [res] = run["results"]
    assert run["summary"] == {"current": {"passed": 1, "total": 1}} and res["passed"]
    assert [c["answered"] for c in res["calls"]] == ["recorded", "recorded"]
    assert not (lab / "restarted-web").exists() and not (lab / "listed").exists()  # nothing touched
    assert res["checks"][2]["note"] == "fine"

    # Without a recording: the read-only tool runs for real, the other one doesn't.
    case = (await hq.http.put(f"/api/evals/cases/{case['id']}", json={**case, "recording": []})).json()
    [res] = (await _run(hq, [case["id"]]))["results"]
    answered = {c["tool"]: c["answered"] for c in res["calls"]}
    assert answered == {"lab_list_vms": "live (safe in tests)",
                        "lab_restart_vm": "not available (not marked safe in tests)"}
    assert (lab / "listed").exists() and not (lab / "restarted-web").exists()
    assert not res["passed"] and "a tool was missing" in [c["note"] for c in res["checks"]]
    # Unticking 'safe in tests' keeps even the read-only tool from running.
    await hq.http.put(f"/api/tool-servers/{srv['id']}", json={**srv, "eval_live": {"list_vms": False}})
    [res] = (await _run(hq, [case["id"]]))["results"]
    assert {c["tool"]: c["answered"] for c in res["calls"]}["lab_list_vms"].startswith("not available")
    tools = {x["name"]: x for x in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert tools["list_vms"]["read_only_hint"] and not tools["list_vms"]["safe_in_tests"]
    assert not tools["restart_vm"]["read_only_hint"]


async def test_ask_and_block_tools_never_run_in_tests(hq, lab):
    await hq.start_worker()
    srv, a = await _lab_agent(hq, lab, approval_rules={"list_vms": "ask"}, eval_live={"restart_vm": True})
    case = (await hq.http.post("/api/evals/cases", json={
        "agent_id": a["id"], "prompt": "restart web", "checks": [{"type": "contains", "value": "restarted"}]})).json()
    [res] = (await _run(hq, [case["id"]]))["results"]
    answered = {c["tool"]: c["answered"] for c in res["calls"]}
    assert answered["lab_list_vms"] == "not available (approval: ask)"
    assert answered["lab_restart_vm"] == "live (safe in tests)"  # you marked it safe; it's Allow
    from app.models import ToolApproval
    with db.session() as s:
        assert s.exec(select(ToolApproval)).all() == []  # no approval request from a test


async def test_feedback_and_corrections_draft_cases(hq):
    await hq.start_worker()
    a = await hq.agent(name="Duke", model="fake:echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "summarise the backups")
    await hq.wait_agent_replies(t["id"], 1)
    reply = next(m for m in hq.messages(t["id"]) if m.role.value == "agent")
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": "down", "comment": "failures first"})
    await hq.say(t["id"], "No, put it in a table")
    await hq.wait_agent_replies(t["id"], 2)
    await hq.say(t["id"], "and the UPS?")
    await hq.wait_agent_replies(t["id"], 3)
    third = [m for m in hq.messages(t["id"]) if m.role.value == "agent"][-1]
    ctx = (await hq.http.post("/api/evals/cases/from-message", json={"message_id": third.id})).json()["context"]
    assert ctx.startswith("User: summarise the backups") and ctx.endswith("You: You said: No, put it in a table")
    assert "and the UPS?" not in ctx and "User: No, put it in a table" in ctx
    out = (await hq.http.get("/api/evals/cases?status=draft")).json()
    [case] = [c for c in out["cases"] if c["source"] != "reply"]  # one draft per job: the correction added to it
    assert out["counts"] == {"draft": 2} and case["source"] == "feedback"
    assert [c["type"] for c in case["checks"]] == ["rubric", "rubric"]
    assert "failures first" in case["checks"][0]["value"] and "put it in a table" in case["checks"][1]["value"]
    # Once kept, a new note starts a fresh draft; the kept test doesn't change.
    kept = (await hq.http.put(f"/api/evals/cases/{case['id']}", json={**case, "status": "active"})).json()
    await hq.http.post(f"/api/messages/{reply.id}/feedback", json={"rating": "down", "comment": "and shorter"})
    out = (await hq.http.get("/api/evals/cases")).json()
    assert out["counts"] == {"draft": 2, "active": 1}
    assert (await hq.http.get(f"/api/evals/cases/{case['id']}")).json()["checks"] == kept["checks"]
    # "Make this a test" on that reply opens the existing case rather than another.
    again = (await hq.http.post("/api/evals/cases/from-message", json={"message_id": reply.id})).json()
    assert again["id"] in {c["id"] for c in out["cases"]}
    await hq.http.put("/api/evals/settings", json={"draft_from_feedback": False})
    await hq.say(t["id"], "No, shorter")
    await hq.wait_agent_replies(t["id"], 3)
    assert len((await hq.http.get("/api/evals/cases")).json()["cases"]) == 3


async def _style_skill(hq, body):
    return (await hq.http.post("/api/skills", json={
        "name": "house_style", "title": "House style", "when_to_use": "When writing any report or answer.",
        "body": body})).json()


async def test_auto_mode_applies_a_skill_change_only_if_tests_dont_get_worse(hq):
    await hq.http.put("/api/evals/settings", json={"judge_model": "fake:judge"})
    await _style_skill(hq, "Use bullet lists for everything you write.")
    a = await hq.agent(name="Duke", model="fake:skilled", learning_mode="auto")
    t = await hq.thread(a["id"])
    case = (await hq.http.post("/api/evals/cases", json={
        "agent_id": a["id"], "prompt": "write the report", "skill_names": ["house_style"],
        "checks": [{"type": "contains", "value": "TABLES FIRST"}, {"type": "skill_loaded", "value": "house_style"}]})).json()
    me = load_agent(a["id"])

    got = await hq.runtime.submit_skill_proposal(
        me, name="house_style", title="House style", when_to_use="When writing any report or answer.",
        steps="TABLES FIRST, then a short summary.", reason="Mike asked for tables.", revise="house_style",
        thread_id=t["id"])
    assert got["status"] == "testing", got
    with db.session() as s:
        assert s.exec(select(Skill).where(Skill.name == "house_style")).first().version == 1  # not yet
    [run] = await evals.run_due(hq.runtime)
    assert run.summary == {"without": {"passed": 0, "total": 1}, "with": {"passed": 1, "total": 1}}
    with db.session() as s:
        p = s.get(SkillProposal, got["proposal_id"])
        k = s.exec(select(Skill).where(Skill.name == "house_style")).first()
    assert p.status == "applied" and k.version == 2 and "TABLES FIRST" in k.body
    assert p.eval_summary["regressed"] is False
    msg = next(m for m in hq.messages(t["id"]) if m.meta.get("proposal_id") == p.id)
    assert msg.meta["status"] == "applied" and msg.meta["eval_summary"]["with"]["passed"] == 1

    # A change that makes the test fail waits for you instead.
    got = await hq.runtime.submit_skill_proposal(
        me, name="house_style", title="House style", when_to_use="When writing any report or answer.",
        steps="Write everything in flowing prose paragraphs.", reason="Trying prose.", revise="house_style", thread_id=t["id"])
    await evals.run_due(hq.runtime)
    with db.session() as s:
        p = s.get(SkillProposal, got["proposal_id"])
        k = s.exec(select(Skill).where(Skill.name == "house_style")).first()
    assert p.status == "pending" and p.eval_summary["regressed"] and k.version == 2
    assert (await hq.http.get("/api/inbox/count")).json()["skills"] == 1
    # The case itself still passes with the current skill.
    [res] = (await _run(hq, [case["id"]]))["results"]
    assert res["passed"]


async def test_propose_mode_shows_the_numbers_and_skips_runs_without_cases(hq):
    await _style_skill(hq, "Use bullet lists for everything you write.")
    a = await hq.agent(name="Duke", model="fake:skilled", learning_mode="propose")
    t = await hq.thread(a["id"])
    me = load_agent(a["id"])
    got = await hq.runtime.submit_skill_proposal(
        me, name="house_style", title="House style", when_to_use="When writing any report.", steps="TABLES FIRST, then a short summary.",
        reason="r", revise="house_style", thread_id=t["id"])
    assert got["status"] == "pending"
    assert await evals.run_due(hq.runtime) == []  # no cases: nothing to run
    await hq.http.post("/api/evals/cases", json={
        "agent_id": a["id"], "prompt": "write it", "checks": [{"type": "contains", "value": "TABLES FIRST"}]})
    got = await hq.runtime.submit_skill_proposal(
        me, name="other_style", title="Other", when_to_use="When writing any report.", steps="TABLES FIRST, then a short summary.",
        reason="r", thread_id=t["id"])
    [run] = await evals.run_due(hq.runtime)
    with db.session() as s:
        p = s.get(SkillProposal, got["proposal_id"])
    assert p.status == "pending" and p.eval_summary["with"] == run.summary["with"]
