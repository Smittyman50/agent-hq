"""Self-forming teams on standing goals: an agent allowed sub-agents, with a team size above
1, hands startable next actions to sub-agents that work them in parallel. Dependencies
between actions, team slots, results landing on actions (no report turns), a failed result
blocking its action for the lead, the runner waiting on the team only when the lead has
nothing to do, the done check, approval mode, and Stop."""

from __future__ import annotations

import asyncio
import re
import time

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from app import db, workitems as wi
from app.models import Job, JobKind, WorkItem, WorkStatus

from .conftest import SCRIPTS, _last_prompt, _tool_returns

TEAM: dict = {"fail": set(), "tasks": [], "delay": 1.0, "tools": []}


def _returns(messages) -> dict:
    out = {}
    for r in reversed(_tool_returns(messages)):
        out.setdefault(r.tool_name, []).append(r.content)
    return out


def _last(messages):
    rs = _tool_returns(messages)
    return (rs[0].tool_name, rs[0].content) if rs else (None, None)


async def team_lead(messages, info):
    """As a goal's lead: plans A and B (parallel) and C (after both), hands A and B to its
    team, then does C itself and claims done. A blocked action gets dropped. As a team
    member (the same model): does its action, or fails the ones listed in TEAM["fail"]."""
    prompt = _last_prompt(messages)
    if prompt.startswith("You're on"):
        TEAM["tasks"].append(prompt)
        await asyncio.sleep(TEAM["delay"])
        action = re.search(r"^Your action: (.+)$", prompt, re.M).group(1)
        out = info.output_tools[0].name
        if action in TEAM["fail"]:
            return ModelResponse(parts=[ToolCallPart(out, {"status": "failed",
                                                           "summary": f"couldn't reach the switch for {action}"})])
        return ModelResponse(parts=[ToolCallPart(out, {"status": "done", "summary": f"found 3 VLANs for {action}",
                                                       "details": "10, 20, 30"})])
    if not prompt.startswith("[Goal turn"):
        return ModelResponse(parts=[TextPart("ok")])
    if info.function_tools:
        TEAM["tools"].append(sorted(t.name for t in info.function_tools))
    name, content = _last(messages)
    got = _returns(messages)
    if "(none yet" in prompt:
        if name is None:
            return ModelResponse(parts=[ToolCallPart("add_action", {"title": "Audit core switch"})])
        adds = got.get("add_action", [])
        if name == "add_action" and len(adds) == 1:
            return ModelResponse(parts=[ToolCallPart("add_action", {"title": "Audit edge switch"})])
        if name == "add_action" and len(adds) == 2:
            return ModelResponse(parts=[ToolCallPart("add_action", {
                "title": "Write the VLAN notes", "after": [adds[0]["action_id"], adds[1]["action_id"]]})])
        if name == "add_action" and len(adds) == 3:
            return ModelResponse(parts=[ToolCallPart("assign_action", {
                "action_id": adds[0]["action_id"], "role": "Core auditor", "instructions": "Use SNMP."})])
        if name == "assign_action" and len(got["assign_action"]) == 1:
            return ModelResponse(parts=[ToolCallPart("assign_action", {
                "action_id": adds[1]["action_id"], "role": "Edge auditor"})])
        return ModelResponse(parts=[TextPart(f"Team at work: {content}")])
    # Later turns: drop a blocked action, else start the next to-do, finish it, claim done.
    blocked = re.search(r"^- \[blocked[^\]]*\] (\w+):", prompt, re.M)
    todo = [m for m in re.finditer(r"^- \[to do([^\]]*)\] (\w+):", prompt, re.M) if "waiting for" not in m.group(1)]
    mine = re.search(r"^- \[in progress\] (\w+):", prompt, re.M)
    if name is None:
        if blocked:
            return ModelResponse(parts=[ToolCallPart("drop_action", {"action_id": blocked.group(1),
                                                                     "reason": "the other audit covers it"})])
        if mine:
            return ModelResponse(parts=[ToolCallPart("complete_action", {"action_id": mine.group(1), "result": "notes written"})])
        if todo:
            return ModelResponse(parts=[ToolCallPart("start_action", {"action_id": todo[0].group(2)})])
        return ModelResponse(parts=[ToolCallPart("claim_done", {"evidence": "Audits done and notes written."})])
    if name == "drop_action":
        return ModelResponse(parts=[TextPart("Dropped it; carrying on next turn.")])
    if name == "start_action":
        aid = todo[0].group(2) if todo else None
        return ModelResponse(parts=[ToolCallPart("complete_action", {"action_id": aid, "result": "notes written"})])
    if name == "complete_action":
        return ModelResponse(parts=[ToolCallPart("claim_done", {"evidence": "Audits done and notes written."})])
    return ModelResponse(parts=[TextPart(f"Status: {content}")])


SCRIPTS["team-lead"] = team_lead

LIMITS = {"concurrent": 3, "per_task": 3, "per_day": 20, "max_minutes": 5, "max_requests": 10}


async def _lead(hq, **kw) -> dict:
    body = {"name": "Mainframe", "model": "fake:team-lead", "subagent_mode": "allowed",
            "subagent_limits": LIMITS, "goal_team_size": 3, **kw}
    return await hq.agent(**body)


async def _goal(hq, agent_id: str) -> dict:
    r = await hq.http.post("/api/work-items", json={
        "title": "Document the VLANs", "agent_id": agent_id, "objective": "Know every VLAN.",
        "done_criteria": "A note per VLAN.", "status": "ready"})
    assert r.status_code == 201, r.text
    return r.json()


def _g(goal_id: str) -> WorkItem:
    return wi.get(goal_id)


def _acts(goal_id: str) -> dict[str, WorkItem]:
    return {a.title: a for a in wi.actions(goal_id)}


async def run_until(hq, pred, timeout: float = 25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await wi.tick(hq.runtime)
        if pred():
            return
        await asyncio.sleep(0.1)
    g = [(x.title, x.status, x.waiting_on) for x in _all_goals()]
    acts = [(a.title, a.status, a.job_id, a.result) for x in _all_goals() for a in wi.actions(x.id)]
    raise AssertionError(f"timed out; goals={g} actions={acts}")


def _all_goals():
    from sqlmodel import select
    with db.session() as s:
        return list(s.exec(select(WorkItem).where(WorkItem.kind == "goal")).all())


def _reset():
    TEAM.update(fail=set(), tasks=[], delay=1.0, tools=[])


# --------------------------------------------------------------------------- the loop

async def test_team_works_parallel_actions_then_the_lead_finishes(hq):
    _reset()
    await hq.start_worker(concurrency=6)
    lead = await _lead(hq)
    g = await _goal(hq, lead["id"])
    # Both audits go to the team; the notes wait for them; the lead waits on its team.
    await run_until(hq, lambda: (_g(g["id"]).waiting_on or {}).get("kind") == "team")
    acts = _acts(g["id"])
    core, edge, notes = acts["Audit core switch"], acts["Audit edge switch"], acts["Write the VLAN notes"]
    assert notes.depends_on == [core.id, edge.id] and notes.status == WorkStatus.backlog
    assert wi.on_team(core) and wi.on_team(edge) and core.assignee_agent_id != edge.assignee_agent_id
    card = wi.card(_g(g["id"]))
    assert card["team"] == {"size": 3, "working": 2, "free_slots": 0}
    by_id = {a["id"]: a for a in card["actions"]}
    assert by_id[notes.id]["waiting_for"] == [core.id, edge.id]
    assert by_id[core.id]["worked_by"].startswith("Core auditor (Mainframe #")
    # The lead got assign_action; team members got their goal and action, not the chat.
    assert "assign_action" in TEAM["tools"][0]
    assert any("Your action: Audit core switch" in t and "Know every VLAN." in t and "Use SNMP." in t
               for t in TEAM["tasks"])
    # Then the results land, the lead writes the notes itself and claims done.
    await run_until(hq, lambda: _g(g["id"]).status == WorkStatus.review)
    acts = _acts(g["id"])
    assert acts["Audit core switch"].status == WorkStatus.done
    assert acts["Audit core switch"].result.startswith("found 3 VLANs for Audit core switch")
    assert acts["Write the VLAN notes"].status == WorkStatus.done and acts["Write the VLAN notes"].assignee_agent_id is None
    log = [e.text for e in wi.events(g["id"])]
    assert any(t.startswith("Assigned to Core auditor") for t in log)
    assert any(e.actor.startswith("Edge auditor") and e.text.startswith("Done: Audit edge switch")
               for e in wi.events(g["id"]))
    # No report turns for team results: the board carries them.
    reports = [j for j in hq.jobs() if j.kind == JobKind.turn and (j.meta or {}).get("report_for")]
    assert reports == []
    # The second turn's card showed who did what.
    assert _g(g["id"]).turns >= 2
    d = (await hq.http.get(f"/api/work-items/{g['id']}")).json()
    by_title = {a["title"]: a for a in d["actions"]}
    assert by_title["Audit core switch"]["assignee"].startswith("Core auditor")
    assert by_title["Write the VLAN notes"]["after"] == [core.id, edge.id]
    assert d["team_size"] == 3 and d["team_working"] == 0


async def test_a_failed_team_result_blocks_its_action_for_the_lead(hq):
    _reset()
    TEAM["fail"] = {"Audit edge switch"}
    await hq.start_worker(concurrency=6)
    lead = await _lead(hq)
    g = await _goal(hq, lead["id"])
    await run_until(hq, lambda: _acts(g["id"]).get("Audit edge switch") is not None
                    and _acts(g["id"])["Audit edge switch"].status in (WorkStatus.blocked, WorkStatus.dropped))
    edge = _acts(g["id"])["Audit edge switch"]
    assert "couldn't reach the switch" in edge.result
    # The lead re-plans (drops it), the notes become startable, and it finishes.
    await run_until(hq, lambda: _g(g["id"]).status == WorkStatus.review)
    acts = _acts(g["id"])
    assert acts["Audit edge switch"].status == WorkStatus.dropped
    assert acts["Write the VLAN notes"].status == WorkStatus.done


# --------------------------------------------------------------------------- rules

async def test_dependencies_and_team_slots(hq):
    lead = await _lead(hq, goal_team_size=2)
    g = await _goal(hq, lead["id"])
    wi.pull(lead["id"])
    a = wi.new_action(g["id"], "First", actor="x")
    b = wi.new_action(g["id"], "Second", actor="x")
    c = wi.new_action(g["id"], "Third", actor="x", after=[a.id])
    try:
        wi.new_action(g["id"], "Bad", actor="x", after=["nope"])
        raise AssertionError("unknown dependency accepted")
    except wi.WorkError as e:
        assert "no action nope" in str(e)
    try:
        wi.set_action(g["id"], c.id, WorkStatus.in_progress, actor="x")
        raise AssertionError("started before its dependency")
    except wi.WorkError as e:
        assert "comes after 'First'" in str(e)
    # Team size 2 = the lead plus one sub-agent.
    wi.reserve_for_team(g["id"], a.id, actor="x")
    try:
        wi.reserve_for_team(g["id"], b.id, actor="x")
        raise AssertionError("team over its size")
    except wi.WorkError as e:
        assert "team is full" in str(e)
    # The lead still works one of its own alongside the team.
    wi.set_action(g["id"], b.id, WorkStatus.in_progress, actor="x")
    try:
        wi.set_action(g["id"], a.id, WorkStatus.done, actor="x")
        raise AssertionError("the lead finished its team's action")
    except wi.WorkError as e:
        assert "sub-agent on your team is working" in str(e)
    # A reservation that couldn't become a sub-agent is released.
    wi.release_team(g["id"], a.id)
    assert _acts(g["id"])["First"].status == WorkStatus.backlog and not _acts(g["id"])["First"].job_id


async def test_no_team_without_sub_agents(hq):
    agent = await hq.agent(name="Solo", model="fake:team-lead", goal_team_size=3)  # sub-agents off
    from app.runtime import load_agent
    assert wi.team_size(load_agent(agent["id"])) == 1
    g = await _goal(hq, agent["id"])
    wi.pull(agent["id"])
    a = wi.new_action(g["id"], "First", actor="x")
    try:
        wi.reserve_for_team(g["id"], a.id, actor="x")
        raise AssertionError("assigned without a team")
    except wi.WorkError as e:
        assert "no team" in str(e)
    lanes = (await hq.http.get("/api/board")).json()
    assert next(x for x in lanes["goals"] if x["id"] == g["id"])["team_size"] == 1


async def test_team_size_is_capped_by_the_sub_agent_limit(hq):
    from app.runtime import load_agent

    lead = await _lead(hq, goal_team_size=5, subagent_limits={**LIMITS, "concurrent": 2})
    assert wi.team_size(load_agent(lead["id"])) == 3  # itself + 2 at once
    ask = await _lead(hq, name="Asker", goal_team_size=5, subagent_mode="ask",
                      subagent_limits={**LIMITS, "concurrent": 1})
    assert wi.team_size(load_agent(ask["id"])) == 5  # your grant decides in ask mode


async def test_ask_mode_needs_your_ok_and_releases_the_action(hq):
    """In "Ask me first" mode, assign_action is refused until you approve a staffing request;
    the action isn't left reserved, and the lead does the work itself instead."""
    _reset()
    await hq.start_worker(concurrency=4)
    lead = await _lead(hq, subagent_mode="ask")
    g = await _goal(hq, lead["id"])
    await run_until(hq, lambda: _g(g["id"]).status == WorkStatus.review)
    acts = _acts(g["id"])
    assert not [j for j in hq.jobs() if (j.meta or {}).get("goal_action")]
    assert all(a.assignee_agent_id is None for a in acts.values())
    assert not any(e.text.startswith("Assigned to") for e in wi.events(g["id"]))
    assert acts["Audit core switch"].status == WorkStatus.done  # the lead did it itself


async def test_claim_done_waits_for_the_team(hq):
    from types import SimpleNamespace

    lead = await _lead(hq)
    g = await _goal(hq, lead["id"])
    wi.pull(lead["id"])
    a = wi.new_action(g["id"], "First", actor="x")
    wi.reserve_for_team(g["id"], a.id, actor="x")
    ts = wi.goal_toolset(hq.runtime, team=True)
    claim = ts.tools["claim_done"].function
    ctx = SimpleNamespace(deps=SimpleNamespace(agent_id=lead["id"], thread_id=_g(g["id"]).thread_id, depth=0))
    out = await claim(ctx, "all done")
    assert "team is still working" in out["error"]
    assert "assign_action" in ts.tools and "assign_action" not in wi.goal_toolset(hq.runtime).tools


async def test_stop_puts_team_actions_back_to_do(hq):
    _reset()
    TEAM["delay"] = 30
    await hq.start_worker(concurrency=6)
    lead = await _lead(hq)
    g = await _goal(hq, lead["id"])
    await run_until(hq, lambda: sum(1 for a in wi.actions(g["id"]) if wi.on_team(a)) == 2
                    and all(a.job_id != wi.PENDING for a in wi.actions(g["id"])))
    r = await hq.http.post(f"/api/threads/{_g(g['id']).thread_id}/stop")
    assert r.status_code == 200
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(wi.on_team(a) for a in wi.actions(g["id"])):
        await asyncio.sleep(0.1)
    acts = _acts(g["id"])
    assert acts["Audit core switch"].status == WorkStatus.backlog and not acts["Audit core switch"].job_id
    assert _g(g["id"]).status == WorkStatus.blocked
    assert any(t.startswith("Stopped: Audit") for t in (e.text for e in wi.events(g["id"])))
    with db.session() as s:
        from sqlmodel import select
        team_jobs = s.exec(select(Job).where(Job.origin_thread_id == _g(g["id"]).thread_id)).all()
    assert all(j.status.value in ("done", "failed") for j in team_jobs)
