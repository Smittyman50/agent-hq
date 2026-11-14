"""Standing goals (app/workitems.py): the board, WIP limits, pulling from Ready (never from
Backlog), the goal runner and its card prompt, one action at a time, review, waiting on
you, on time and on background delegations, the guards (no progress, limits), Stop, agent
proposals, where the goal tools appear, and the API."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

from app import db, workitems as wi
from app.models import WorkItem, WorkStatus

from .conftest import SCRIPTS, _last_prompt, _tool_returns, worker_fn

SEEN: dict[str, list] = {"prompts": [], "tools": []}


def _returns(messages) -> dict:
    """Tool name -> content, for the calls made since the latest prompt (newest wins)."""
    out = {}
    for r in reversed(_tool_returns(messages)):
        out[r.tool_name] = r.content
    return out


def _last_tool(messages):
    rs = _tool_returns(messages)
    return (rs[0].tool_name, rs[0].content) if rs else (None, None)


def goaler(messages, info):
    """Works a goal by the book: look at the card, add an action, start it, finish it,
    claim done. Outside goal turns it just chats."""
    prompt = _last_prompt(messages)
    if info.function_tools:  # not a chat-titling call (those have no tools)
        SEEN["tools"].append(sorted(t.name for t in info.function_tools))
    if not prompt.startswith("[Goal turn"):
        return ModelResponse(parts=[TextPart(f"chat: {prompt[:40]}")])
    SEEN["prompts"].append(prompt)
    name, content = _last_tool(messages)
    if name is None:
        return ModelResponse(parts=[ToolCallPart("board_view", {})])
    if name == "board_view":
        return ModelResponse(parts=[ToolCallPart("add_action", {"title": "List the VLANs on the core switch"})])
    if name == "add_action":
        return ModelResponse(parts=[ToolCallPart("start_action", {"action_id": content["action_id"]})])
    if name == "start_action":
        aid = _returns(messages)["add_action"]["action_id"]
        return ModelResponse(parts=[ToolCallPart("complete_action", {"action_id": aid, "result": "VLANs 10, 20, 30"})])
    if name == "complete_action":
        return ModelResponse(parts=[ToolCallPart("claim_done", {"evidence": "All three VLANs are documented."})])
    return ModelResponse(parts=[TextPart(f"Goal status: {content.get('summary', content)}")])


def idler(messages, info):
    """Talks about the goal but never touches the board."""
    SEEN["prompts"].append(_last_prompt(messages))
    return ModelResponse(parts=[TextPart("Still thinking about it.")])


def asker(messages, info):
    """First goal turn: asks you a question and waits. Your answer: thanks. Next goal turn:
    claims done with what you said."""
    prompt = _last_prompt(messages)
    name, _ = _last_tool(messages)
    if not prompt.startswith("[Goal turn"):
        return ModelResponse(parts=[TextPart("Thanks, noted.")])
    asked = any(p.part_kind == "tool-call" and p.tool_name == "wait_for"
                for m in messages for p in getattr(m, "parts", []))
    if name is None and not asked:
        return ModelResponse(parts=[ToolCallPart("wait_for", {"kind": "user", "reason": "Which VLAN is the IoT one?"})])
    if name is None:
        return ModelResponse(parts=[ToolCallPart("claim_done", {"evidence": "The user says VLAN 30."})])
    return ModelResponse(parts=[TextPart("Over to you.")])


def delegator(messages, info):
    """First goal turn: hands the work to the first teammate in the background. Next goal
    turn (after the report): claims done."""
    import re

    prompt = _last_prompt(messages)
    name, _ = _last_tool(messages)
    if prompt.startswith("[Background job"):
        return ModelResponse(parts=[TextPart("The teammate reported back.")])
    if not prompt.startswith("[Goal turn"):
        return ModelResponse(parts=[TextPart("chat")])
    delegated = any(p.part_kind == "tool-call" and p.tool_name == "delegate"
                    for m in messages for p in getattr(m, "parts", []))
    roster = (info.instructions or "").partition("Team members you can delegate to:")[2]
    team = re.findall(r"^- ([^:]+):", roster, re.M)
    if name is None and not delegated:
        return ModelResponse(parts=[ToolCallPart("delegate", {"agent_name": team[0], "task": "Count the VLANs",
                                                              "background": True})])
    if name is None:
        return ModelResponse(parts=[ToolCallPart("claim_done", {"evidence": "The teammate counted them."})])
    return ModelResponse(parts=[TextPart("Underway.")])


async def slowish(messages, info):
    if not _tool_returns(messages):
        await asyncio.sleep(1.5)
    return worker_fn(messages, info)


def proposer_of_goals(messages, info):
    name, content = _last_tool(messages)
    if not info.function_tools:  # a chat-titling call
        return ModelResponse(parts=[TextPart("VLAN notes")])
    SEEN["tools"].append(sorted(t.name for t in info.function_tools))
    if name is None:
        return ModelResponse(parts=[ToolCallPart("propose_goal", {
            "title": "Document every VLAN", "objective": "Every VLAN has a note in the vault.",
            "done_criteria": "One note per VLAN with ID, subnet and purpose.",
            "reason": "I kept guessing VLAN purposes today."})])
    return ModelResponse(parts=[TextPart(f"proposed: {content}")])


SCRIPTS.update({"goal-worker": goaler, "goal-idler": idler, "goal-asker": asker, "goal-delegator": delegator,
                "goal-slow-helper": slowish, "goal-proposer": proposer_of_goals})


# --------------------------------------------------------------------------- helpers

async def _goal(hq, agent_id: str, status: str = "ready", **kw) -> dict:
    body = {"title": "Document the VLANs", "agent_id": agent_id,
            "objective": "Know what each VLAN is for.", "done_criteria": "Each VLAN listed with its purpose.",
            "status": status, **kw}
    r = await hq.http.post("/api/work-items", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _status(goal_id: str) -> WorkItem:
    return wi.get(goal_id)


async def run_until(hq, pred, timeout: float = 20):
    """Run the goal runner (as the API process would) until `pred()` holds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await wi.tick(hq.runtime)
        if pred():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out; goals={[(g.title, g.status, g.waiting_on, g.turns) for g in _all()]}")


def _all() -> list[WorkItem]:
    from sqlmodel import select
    with db.session() as s:
        return list(s.exec(select(WorkItem).where(WorkItem.kind == "goal")).all())


async def _idle(hq, thread_id: str, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not wi._active_jobs(thread_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("still running")


# --------------------------------------------------------------------------- the loop

async def test_goal_runs_to_review_and_you_confirm(hq):
    SEEN["prompts"].clear()
    await hq.start_worker()
    agent = await hq.agent(name="Scribe", model="fake:goal-worker")
    g = await _goal(hq, agent["id"])
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.review)
    goal = _status(g["id"])
    # The runner pulled it from Ready, opened its conversation, and gave the agent the card.
    assert goal.thread_id and goal.started_at and goal.turns == 1
    p = SEEN["prompts"][0]
    assert p.startswith(f'[Goal turn 1 of up to 40: "Document the VLANs" ({g["id"]})]')
    assert "Know what each VLAN is for." in p and "Each VLAN listed with its purpose." in p
    assert "none yet" in p  # no actions yet
    acts = wi.actions(g["id"])
    assert [(a.title, a.status, a.result) for a in acts] == [
        ("List the VLANs on the core switch", WorkStatus.done, "VLANs 10, 20, 30")]
    assert goal.result == "All three VLANs are documented."
    # The claim is waiting for you, in Requests and on the board.
    count = (await hq.http.get("/api/inbox/count")).json()
    assert count["goals"] == 1 and count["total"] >= 1
    att = (await hq.http.get("/api/work-items/attention")).json()
    assert [(x["id"], x["needs_you"]) for x in att] == [(g["id"], "review")]
    # Nothing more is queued while it waits for you.
    await _idle(hq, goal.thread_id)
    await wi.tick(hq.runtime)
    assert _status(g["id"]).turns == 1
    r = await hq.http.post(f"/api/work-items/{g['id']}/review", json={"approve": True, "note": "Nice"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "done" and d["finished_at"]
    kinds = [e["kind"] for e in d["events"]]
    assert kinds[0] == "created" and "action_added" in kinds and kinds[-1] == "status"
    texts = [m.content for m in hq.messages(goal.thread_id)]
    assert "Goal turn 1: Document the VLANs" in texts
    assert "Scribe says the goal is done." in texts
    assert any(t.startswith("You confirmed the goal is done.") for t in texts)
    assert (await hq.http.get("/api/inbox/count")).json()["goals"] == 0


async def test_sent_back_goal_gets_your_note_on_its_next_turn(hq):
    SEEN["prompts"].clear()
    await hq.start_worker()
    agent = await hq.agent(name="Scribe", model="fake:goal-worker")
    g = await _goal(hq, agent["id"])
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.review)
    r = await hq.http.post(f"/api/work-items/{g['id']}/review",
                           json={"approve": False, "note": "You missed VLAN 40"})
    assert r.json()["status"] == "in_progress"
    await run_until(hq, lambda: _status(g["id"]).turns == 2 and _status(g["id"]).status == WorkStatus.review)
    second = SEEN["prompts"][-1]
    assert second.startswith("[Goal turn 2") and "sent your last done claim back: You missed VLAN 40" in second
    assert "[done]" in second and "VLANs 10, 20, 30" in second  # the earlier action is on the card


# --------------------------------------------------------------------------- WIP and pulling

async def test_wip_limit_pull_order_and_backlog(hq):
    agent = await hq.agent(name="Scribe", model="fake:goal-idler")  # goal_wip defaults to 1
    assert agent["goal_wip"] == 1
    low = await _goal(hq, agent["id"], title="Low", priority=0)
    high = await _goal(hq, agent["id"], title="High", priority=5)
    parked = await _goal(hq, agent["id"], title="Parked", status="backlog", priority=9)
    started = wi.pull(agent["id"])
    assert [x.title for x in started] == ["High"]  # priority first; one slot
    assert _status(low["id"]).status == WorkStatus.ready
    assert _status(parked["id"]).status == WorkStatus.backlog  # never pulled from Backlog
    r = await hq.http.post(f"/api/work-items/{low['id']}/move", json={"status": "in_progress"})
    assert r.status_code == 409 and "WIP limit" in r.json()["detail"]
    # Raise the limit: the next pull fills the new slot.
    r = await hq.http.put(f"/api/agents/{agent['id']}", json={**agent, "goal_wip": 2})
    assert r.status_code == 200, r.text
    assert [x.title for x in wi.pull(agent["id"])] == ["Low"]
    board = (await hq.http.get("/api/board")).json()
    lane = next(a for a in board["agents"] if a["id"] == agent["id"])
    assert (lane["goal_wip"], lane["wip_used"]) == (2, 2)
    assert {x["title"]: x["column"] for x in board["goals"]} == {
        "High": "In progress", "Low": "In progress", "Parked": "Backlog"}
    # Finishing one frees its slot for the next Ready goal.
    third = await _goal(hq, agent["id"], title="Third")
    assert wi.pull(agent["id"]) == []
    await hq.http.post(f"/api/work-items/{high['id']}/move", json={"status": "dropped"})
    assert [x.title for x in wi.pull(agent["id"])] == ["Third"]
    del third


async def test_an_agent_with_no_goal_slots_takes_no_goals(hq):
    agent = await hq.agent(name="Router", model="fake:goal-idler", goal_wip=0)
    g = await _goal(hq, agent["id"])
    assert wi.pull(agent["id"]) == []
    r = await hq.http.post(f"/api/work-items/{g['id']}/move", json={"status": "in_progress"})
    assert r.status_code == 422 and "doesn't take goals" in r.json()["detail"]


async def test_one_action_at_a_time(hq):
    agent = await hq.agent(name="Scribe", model="fake:goal-idler")
    g = await _goal(hq, agent["id"], status="backlog")
    a1 = wi.new_action(g["id"], "First", actor="Scribe")
    a2 = wi.new_action(g["id"], "Second", actor="Scribe")
    wi.set_action(g["id"], a1.id, WorkStatus.in_progress, actor="Scribe")
    try:
        wi.set_action(g["id"], a2.id, WorkStatus.in_progress, actor="Scribe")
        raise AssertionError("two actions in progress")
    except wi.WorkError as e:
        assert "one action at a time" in str(e) and a1.id in str(e)
    wi.set_action(g["id"], a1.id, WorkStatus.done, actor="Scribe", result="ok")
    wi.set_action(g["id"], a2.id, WorkStatus.in_progress, actor="Scribe")
    # You can work the actions from the API too.
    r = await hq.http.patch(f"/api/work-items/{g['id']}/actions/{a2.id}", json={"status": "done", "result": "by hand"})
    assert [a["label"] for a in r.json()["actions"]] == ["done", "done"]


# --------------------------------------------------------------------------- guards

async def test_no_progress_blocks_the_goal_and_you_resume_it(hq):
    await hq.start_worker()
    agent = await hq.agent(name="Ponder", model="fake:goal-idler")
    g = await _goal(hq, agent["id"], limits={"stall_turns": 2})
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.blocked)
    goal = _status(g["id"])
    assert goal.turns == 2 and goal.waiting_on["kind"] == "stalled"
    assert "No progress on the board in 2 goal turns" in goal.waiting_on["detail"]
    assert (await hq.http.get("/api/inbox/count")).json()["goals"] == 1
    assert any("Goal blocked" in m.content for m in hq.messages(goal.thread_id))
    # Blocked still holds its WIP slot (Kanban: fix it or move it).
    assert wi.wip_used(agent["id"]) == 1
    r = await hq.http.post(f"/api/work-items/{g['id']}/move", json={"status": "in_progress"})
    assert r.status_code == 200 and r.json()["stalled_turns"] == 0
    assert any(m.content.startswith("You resumed the goal") for m in hq.messages(goal.thread_id))


async def test_turn_limit_blocks_the_goal(hq):
    await hq.start_worker()
    agent = await hq.agent(name="Ponder", model="fake:goal-idler")
    g = await _goal(hq, agent["id"], limits={"max_turns": 2, "stall_turns": 10})
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.blocked)
    goal = _status(g["id"])
    assert goal.turns == 2 and goal.waiting_on["kind"] == "limit"
    assert "limit of 2 goal turns" in goal.waiting_on["detail"]


async def test_bad_limits_and_missing_fields_are_explained(hq):
    agent = await hq.agent(name="Scribe", model="fake:goal-idler")
    r = await hq.http.post("/api/work-items", json={"title": "x", "agent_id": agent["id"], "objective": "o"})
    assert r.status_code == 422 and "done criteria" in r.json()["detail"]
    r = await hq.http.post("/api/work-items", json={"title": "x", "agent_id": agent["id"], "objective": "o",
                                                    "done_criteria": "d", "limits": {"max_turns": 0}})
    assert r.status_code == 422 and "max_turns" in r.json()["detail"]
    g = await _goal(hq, agent["id"], status="backlog", limits={"max_turns": 40, "max_hours": 6})
    assert g["custom_limits"] == {"max_hours": 6} and g["limits"]["max_hours"] == 6
    r = await hq.http.post(f"/api/work-items/{g['id']}/move", json={"status": "review"})
    assert r.status_code == 422


async def test_stop_blocks_the_goal(hq):
    await hq.start_worker()
    agent = await hq.agent(name="Ponder", model="fake:goal-idler")
    g = await _goal(hq, agent["id"], limits={"stall_turns": 50})
    await run_until(hq, lambda: _status(g["id"]).turns >= 1)
    thread = _status(g["id"]).thread_id
    r = await hq.http.post(f"/api/threads/{thread}/stop")
    assert r.status_code == 200
    goal = _status(g["id"])
    assert goal.status == WorkStatus.blocked and goal.waiting_on["kind"] == "stopped"
    assert wi.needs_you(goal) is None  # you did it; no request for you
    turns = goal.turns
    await _idle(hq, thread)
    for _ in range(3):
        await wi.tick(hq.runtime)
    assert _status(g["id"]).turns == turns


# --------------------------------------------------------------------------- waiting

async def test_waiting_for_you_resumes_when_you_answer(hq):
    await hq.start_worker()
    agent = await hq.agent(name="Curious", model="fake:goal-asker")
    g = await _goal(hq, agent["id"])
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.waiting)
    goal = _status(g["id"])
    assert goal.waiting_on["kind"] == "user" and goal.waiting_on["detail"] == "Which VLAN is the IoT one?"
    assert wi.needs_you(goal) == "question"
    assert any(m.content == "Curious needs you: Which VLAN is the IoT one?" for m in hq.messages(goal.thread_id))
    await _idle(hq, goal.thread_id)
    for _ in range(3):
        await wi.tick(hq.runtime)
    assert _status(g["id"]).turns == 1  # nothing until you answer
    await hq.say(goal.thread_id, "VLAN 30")
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.review)
    assert _status(g["id"]).result == "The user says VLAN 30."
    assert _status(g["id"]).turns == 2


async def test_waiting_for_time(hq):
    agent = await hq.agent(name="Scribe", model="fake:goal-idler")
    g = await _goal(hq, agent["id"])
    wi.pull(agent["id"])
    later = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    wi.set_status(g["id"], WorkStatus.waiting, actor="Scribe", waiting_on={"kind": "time", "until": later})
    assert await wi.advance(hq.runtime, _status(g["id"])) == "waiting: time"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with db.session() as s:
        row = s.get(WorkItem, g["id"])
        row.waiting_on = {**row.waiting_on, "until": past}
        s.add(row)
        s.commit()
    assert await wi.advance(hq.runtime, _status(g["id"])) == "queued"
    assert _status(g["id"]).status == WorkStatus.in_progress


async def test_background_delegation_shows_as_waiting(hq):
    await hq.start_worker()
    helper = await hq.agent(name="Counter", model="fake:goal-slow-helper", description="Counts things")
    lead = await hq.agent(name="Lead", model="fake:goal-delegator", delegates=[helper["id"]])
    g = await _goal(hq, lead["id"])
    await run_until(hq, lambda: (_status(g["id"]).waiting_on or {}).get("kind") == "delegation")
    rev = _status(g["id"]).revision
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.review)
    goal = _status(g["id"])
    assert goal.result == "The teammate counted them."
    assert goal.revision > rev and goal.stalled_turns == 0
    texts = [m.content for m in hq.messages(goal.thread_id)]
    assert "The teammate reported back." in texts


# --------------------------------------------------------------------------- proposals and tools

async def test_agents_propose_goals_and_you_decide(hq):
    SEEN["tools"].clear()
    await hq.start_worker()
    agent = await hq.agent(name="Scout", model="fake:goal-proposer")
    t = await hq.thread(agent["id"])
    await hq.say(t["id"], "what's on VLAN 30?")
    await hq.wait_agent_replies(t["id"], 1)
    # In an ordinary chat it can propose but has no goal tools.
    assert "propose_goal" in SEEN["tools"][0] and "board_view" not in SEEN["tools"][0]
    goals = _all()
    assert len(goals) == 1
    p = goals[0]
    assert p.proposed and p.status == WorkStatus.backlog and p.owner_agent_id == agent["id"]
    assert p.source["agent"] == "Scout" and p.proposal_reason == "I kept guessing VLAN purposes today."
    assert wi.needs_you(p) == "proposal"
    card = next(m for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "goal_proposal")
    assert card.meta["status"] == "proposed"
    r = await hq.http.post(f"/api/work-items/{p.id}/move", json={"status": "ready"})
    assert r.status_code == 422 and "approve" in r.json()["detail"]
    assert wi.pull(agent["id"]) == []
    r = await hq.http.post(f"/api/work-items/{p.id}/proposal", json={"approve": True, "ready": True})
    assert r.json()["status"] == "ready" and not r.json()["proposed"]
    card = next(m for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "goal_proposal")
    assert card.meta["status"] == "approved"


async def test_rejected_proposal_is_dropped(hq):
    agent = await hq.agent(name="Scout", model="fake:goal-idler")
    g = wi.create_goal(title="Rewrite everything", agent_id=agent["id"], objective="o", done_criteria="d",
                       proposed=True, proposal_reason="why not", source={"kind": "agent"}, actor="Scout")
    r = await hq.http.post(f"/api/work-items/{g.id}/proposal", json={"approve": False, "note": "Not now"})
    assert r.json()["status"] == "dropped" and r.json()["review_note"] == "Not now"


async def test_goal_tools_are_only_for_the_owner_in_its_conversation(hq):
    SEEN["tools"].clear()
    await hq.start_worker()
    agent = await hq.agent(name="Scribe", model="fake:goal-worker")
    g = await _goal(hq, agent["id"])
    await run_until(hq, lambda: _status(g["id"]).status == WorkStatus.review)
    goal_tools = SEEN["tools"][0]
    assert {"board_view", "add_action", "start_action", "complete_action", "block_action",
            "drop_action", "log_note", "wait_for", "claim_done", "propose_goal"} <= set(goal_tools)
    # In another conversation, the same agent has no goal tools.
    await _idle(hq, _status(g["id"]).thread_id)  # the goal turn's last reply is in
    SEEN["tools"].clear()
    t = await hq.thread(agent["id"])
    await hq.say(t["id"], "hi")
    await hq.wait_agent_replies(t["id"], 1)
    assert "board_view" not in SEEN["tools"][0]


async def test_goal_tools_refuse_once_the_goal_is_not_active(hq):
    from types import SimpleNamespace

    agent = await hq.agent(name="Scribe", model="fake:goal-idler")
    g = await _goal(hq, agent["id"])
    wi.pull(agent["id"])
    thread = _status(g["id"]).thread_id
    ctx = SimpleNamespace(deps=SimpleNamespace(agent_id=agent["id"], thread_id=thread, depth=0))
    _, _, err = wi._mine(ctx)
    assert err is None
    wi.set_status(g["id"], WorkStatus.blocked, waiting_on={"kind": "stopped"})
    _, _, err = wi._mine(ctx)
    assert "Blocked" in err["error"]
    ctx.deps.depth = 1  # a delegate working in the goal's conversation
    wi.set_status(g["id"], WorkStatus.in_progress)
    _, _, err = wi._mine(ctx)
    assert "no goal of yours" in err["error"]


async def test_editing_a_goal_and_the_detail_view(hq):
    agent = await hq.agent(name="Scribe", model="fake:goal-idler")
    other = await hq.agent(name="Second", model="fake:goal-idler")
    g = await _goal(hq, agent["id"], status="backlog")
    r = await hq.http.patch(f"/api/work-items/{g['id']}", json={"objective": "Know every VLAN's purpose.",
                                                                "owner_agent_id": other["id"]})
    assert r.status_code == 200
    d = r.json()
    assert d["objective"] == "Know every VLAN's purpose." and d["agent_name"] == "Second"
    assert d["events"][-1]["text"].startswith("Changed objective, agent (Second)")
    r = await hq.http.post(f"/api/work-items/{g['id']}/notes", json={"text": "Start with the core switch"})
    assert r.json()["events"][-1]["actor"] == "you"
    # Once it's being worked, the agent can't move.
    wi.set_status(g["id"], WorkStatus.ready)
    wi.pull(other["id"])
    r = await hq.http.patch(f"/api/work-items/{g['id']}", json={"owner_agent_id": agent["id"]})
    assert r.status_code == 422 and "Backlog or Ready" in r.json()["detail"]
    assert (await hq.http.get("/api/work-items/nope")).status_code == 404

