"""Next actions: a request broken into steps, each worked as its own focused run, with the
plan in Valkey and shown live (app/nextactions.py, Runtime.run_with_plan)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo

from app import nextactions
from app.config import reload_settings, settings
from app.models import Role

from .conftest import SCRIPTS, _instructions, _last_prompt

SEEN: dict[str, list] = {}

PLAN = """<think>three parts</think>
BRIEF: Build a small Zork-style text adventure in Python.
1. Write the room map | rooms.py with five rooms and their exits
2. Write the parser | parser.py: go, look, take, inventory
3. Test the game | run a scripted playthrough and fix what breaks"""


def _user_prompts(messages: list[ModelMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, ModelRequest)
               for p in m.parts if isinstance(p, UserPromptPart))


def _first_prompt(messages: list[ModelMessage]) -> str:
    for m in messages:
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt":
                return p.content if isinstance(p.content, str) else str(p.content)
    return ""


def _result(info: AgentInfo, status: str, summary: str, details: str = "") -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "status": status, "summary": summary, "details": details})])


def planner_script(fail_step: int | None = None, revise: str = "KEEP"):
    """Plans three steps; each step makes three requests (two notes, then its result), so a
    run limited to five requests could never do all three in one go."""
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        rules = _instructions(messages)
        prompt = _last_prompt(messages)
        first = _first_prompt(messages)
        if first.startswith("[Next actions: step"):
            prompt = first  # a budget note near the request limit comes in as a later prompt
        if rules.startswith("You plan next actions for"):
            SEEN.setdefault("planned", []).append(prompt)
            return ModelResponse(parts=[TextPart(PLAN)])
        if rules.startswith("You keep the plan of next actions"):
            SEEN.setdefault("revised", []).append(prompt)
            return ModelResponse(parts=[TextPart(revise)])
        if prompt.startswith("[Next actions: step"):
            n = int(prompt.split("step ", 1)[1].split(" ", 1)[0])
            SEEN.setdefault("steps", []).append({"n": n, "prompts": _user_prompts(messages),
                                                 "text": prompt, "rules": rules})
            notes = [p for m in messages if isinstance(m, ModelRequest) for p in m.parts
                     if getattr(p, "tool_name", None) == "note" and p.part_kind == "tool-return"]
            if len(notes) < 2:
                return ModelResponse(parts=[ToolCallPart("note", {"text": f"step {n} note {len(notes) + 1}"})])
            if n == fail_step:
                return _result(info, "failed", f"Step {n} could not finish")
            return _result(info, "done", f"Did step {n}", f"left by step {n}")
        if "[You worked through this request as next actions" in prompt:
            SEEN.setdefault("final", []).append(prompt)
            if info.output_tools:
                return _result(info, "done", "Game built", "")
            return ModelResponse(parts=[TextPart("Your game is ready.")])
        SEEN.setdefault("direct", []).append(prompt)
        if info.output_tools:
            return _result(info, "done", "Did it directly")
        return ModelResponse(parts=[TextPart("Done directly.")])
    fn.plans = True
    return fn


SCRIPTS.update({
    "planner": planner_script(),
    "planner-fails": planner_script(fail_step=2, revise="1. Fix the parser | rewrite parser.py\n2. Test the game | run it"),
    "planner-stops": planner_script(fail_step=1, revise="STOP the map is needed for everything else"),
})


@pytest.fixture
def planning_on(monkeypatch):
    monkeypatch.setenv("AHQ_NEXT_ACTIONS", "1")
    reload_settings()
    SEEN.clear()
    yield
    monkeypatch.setenv("AHQ_NEXT_ACTIONS", "0")
    reload_settings()


# --------------------------------------------------------------------------- parsing

def test_parse_a_plan():
    d = nextactions.parse(PLAN)
    assert d["brief"] == "Build a small Zork-style text adventure in Python."
    assert [s["title"] for s in d["steps"]] == ["Write the room map", "Write the parser", "Test the game"]
    assert d["steps"][1]["details"] == "parser.py: go, look, take, inventory"


def test_direct_and_unusable_answers_run_as_one_run():
    assert nextactions.parse("DIRECT") is None
    assert nextactions.parse("**Direct.**") is None
    assert nextactions.parse("<think>hmm</think>\nDIRECT") is None
    assert nextactions.parse("Sure! I'd just answer that.") is None
    assert nextactions.parse("BRIEF: x\n1. Only one step | do it") is None  # one step = one run


def test_parse_tolerates_small_model_formatting():
    d = nextactions.parse("BRIEF: b\n**1.** Find the feeds: list them\nStep 2) Read each one — note the news\n"
                          "   and keep the links\n- 3: Summarise")
    assert [s["title"] for s in d["steps"]] == ["Find the feeds", "Read each one", "Summarise"]
    assert d["steps"][1]["details"] == "note the news and keep the links"


def test_parse_caps_the_steps():
    text = "BRIEF: b\n" + "\n".join(f"{i}. Step {i} | do {i}" for i in range(1, 15))
    assert len(nextactions.parse(text, max_steps=8)["steps"]) == 8


def test_parse_revision():
    assert nextactions.parse_revision("KEEP") == {"keep": True}
    assert nextactions.parse_revision("Keep going, looks fine") == {"keep": True}
    assert nextactions.parse_revision("STOP: the API is down") == {"stop": "the API is down"}
    assert nextactions.parse_revision("1. Retry with the mirror | use mirror.example")["steps"][0]["title"] == "Retry with the mirror"
    assert nextactions.parse_revision("no idea") == {"keep": True}


def _job(kind="turn", meta=None, **kw):
    return SimpleNamespace(kind=kind, meta={**(meta or {}), **kw})


def test_which_runs_may_plan(planning_on):
    agent = SimpleNamespace(next_actions="auto", is_subagent=False)
    big = "build me a small text adventure game"
    assert nextactions.wants_plan(agent, _job(message_id="m1"), big)
    assert nextactions.wants_plan(agent, _job(meta={"kind": "schedule"}), big)
    assert nextactions.wants_plan(agent, _job("task"), big)
    assert not nextactions.wants_plan(agent, _job(message_id="m1"), "yes thanks")  # too short
    assert not nextactions.wants_plan(agent, _job(), big)  # a report or decision, not your message
    for kind in ("goal", "mqtt", "webhook"):
        assert not nextactions.wants_plan(agent, _job(meta={"message_id": "m1", "kind": kind}), big)
    assert not nextactions.wants_plan(agent, _job(message_id="m1", everyone=True), big)
    assert not nextactions.wants_plan(agent, _job("task", goal_action="a1"), big)
    assert not nextactions.wants_plan(agent, None, big)
    assert not nextactions.wants_plan(agent, _job(message_id="m1"), big, replay=object())
    assert not nextactions.wants_plan(SimpleNamespace(next_actions="off", is_subagent=False), _job(message_id="m1"), big)
    assert not nextactions.wants_plan(SimpleNamespace(next_actions="auto", is_subagent=True), _job("task"), big)
    always = SimpleNamespace(next_actions="always", is_subagent=False)
    assert nextactions.wants_plan(always, _job(message_id="m1"), "fix it")
    settings.next_actions_enabled = False
    assert not nextactions.wants_plan(agent, _job(message_id="m1"), big)


# --------------------------------------------------------------------------- running

async def test_a_request_is_worked_as_next_actions(hq, planning_on):
    await hq.start_worker()
    a = await hq.agent(name="Hi-Tech", model="fake:planner", max_requests=5)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Your game is ready."]
    # Each step is its own run with a fresh request budget: 9 requests in all, under a
    # five-request limit, each step starting from just its own prompt (no chat history).
    assert [s["n"] for s in SEEN["steps"]] == [1, 1, 1, 2, 2, 2, 3, 3, 3]
    firsts = [s for i, s in enumerate(SEEN["steps"]) if i % 3 == 0]
    assert [s["prompts"] for s in firsts] == [1, 1, 1]
    assert "You're working through a request as next actions" in SEEN["steps"][0]["rules"]
    third = next(s["text"] for s in SEEN["steps"] if s["n"] == 3)
    assert "1. [done] Write the room map" in third and "Result: Did step 1" in third
    assert "3. [now] Test the game" in third and "left by step 2" in third
    # The reply is written from the results.
    assert "2. [done] Write the parser: Did step 2" in SEEN["final"][0]
    assert "direct" not in SEEN
    # The plan, in Valkey only, kept for the scratch-pad time once done.
    job = hq.jobs()[-1]
    r = hq.runtime.broker.r
    plan = await nextactions.load(r, job.id)
    assert plan["status"] == "done" and plan["agent"] == "Hi-Tech" and plan["depth"] == 0
    assert [s["status"] for s in plan["steps"]] == ["done", "done", "done"]
    assert 0 < await r.ttl(f"ahq:plan:{job.id}") <= settings.pad_ttl_hours * 3600
    # All the steps' notes on the one scratch pad.
    assert len(await r.lrange(f"ahq:pad:{job.id}", 0, -1)) == 6
    # The panel's endpoint and the note in the chat.
    got = (await hq.http.get(f"/api/threads/{t['id']}/plans")).json()
    assert [p["job_id"] for p in got] == [job.id]
    assert any(m.role == Role.event and (m.meta or {}).get("kind") == "next_actions"
               and "3 next actions" in m.content for m in hq.messages(t["id"]))


async def test_a_failed_step_is_replanned(hq, planning_on):
    await hq.start_worker()
    a = await hq.agent(name="Hi-Tech", model="fake:planner-fails")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    await hq.wait_agent_replies(t["id"], 1)
    plan = await nextactions.load(hq.runtime.broker.r, hq.jobs()[-1].id)
    assert [(s["title"], s["status"]) for s in plan["steps"]] == [
        ("Write the room map", "done"), ("Write the parser", "failed"),
        ("Fix the parser", "done"), ("Test the game", "done")]
    assert plan["revisions"] == 1 and plan["status"] == "partial"
    assert "Step 2 could not finish" in SEEN["revised"][0]


async def test_a_plan_can_stop_early(hq, planning_on):
    await hq.start_worker()
    a = await hq.agent(name="Hi-Tech", model="fake:planner-stops")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    await hq.wait_agent_replies(t["id"], 1)
    plan = await nextactions.load(hq.runtime.broker.r, hq.jobs()[-1].id)
    assert [s["status"] for s in plan["steps"]] == ["failed", "skipped", "skipped"]
    assert plan["stopped_because"] == "the map is needed for everything else"
    assert "The plan stopped early: the map is needed" in SEEN["final"][0]


async def test_quick_messages_and_agents_with_it_off_dont_plan(hq, planning_on):
    await hq.start_worker()
    a = await hq.agent(name="Hi-Tech", model="fake:planner")
    off = await hq.agent(name="Scarlett", model="fake:planner", next_actions="off")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "thanks!")
    t2 = await hq.thread(off["id"])
    await hq.say(t2["id"], "build me a zork inspired text based game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Done directly."]
    assert await hq.wait_agent_replies(t2["id"], 1) == ["Done directly."]
    assert "planned" not in SEEN
    assert (await hq.http.get(f"/api/threads/{t2['id']}/plans")).json() == []


async def test_bad_mode_is_refused(hq):
    r = await hq.http.post("/api/agents", json={"name": "X", "model": "fake:echo", "next_actions": "sometimes"})
    assert r.status_code == 422


async def test_a_delegated_task_plans_and_hands_back_a_result(hq, planning_on):
    await hq.start_worker(concurrency=4)
    worker = await hq.agent(name="Hi-Tech", model="fake:planner", description="coder")
    boss = await hq.agent(name="Duke", model="fake:boss", delegates=[worker["id"]])
    t = await hq.thread(boss["id"])
    await hq.say(t["id"], "get the team to build a text adventure game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Report: Game built"]
    task = next(j for j in hq.jobs() if j.kind.value == "task")
    plan = await nextactions.load(hq.runtime.broker.r, task.id)
    assert plan["kind"] == "task" and plan["depth"] == 1 and plan["status"] == "done"
    assert plan["parent_job_id"] == task.parent_job_id


async def test_a_planner_that_fails_falls_back_to_one_run(hq, planning_on, monkeypatch):
    await hq.start_worker()

    async def broken(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(hq.workers[0].rt, "_ask_planner", broken)
    a = await hq.agent(name="Hi-Tech", model="fake:planner")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Done directly."]


# --------------------------------------------------------------------------- restarts

HANG = {"step2": 0}


def hanging_planner():
    """Like planner_script, but its first try at step 2 hangs: the worker 'dies' there."""
    base = planner_script()

    async def fn(messages, info):
        first = _first_prompt(messages)
        if first.startswith("[Next actions: step 2") and not any(
                getattr(p, "part_kind", "") == "tool-return" for m in messages for p in getattr(m, "parts", [])):
            HANG["step2"] += 1
            SEEN.setdefault("step2_prompts", []).append(first)
            if HANG["step2"] == 1:
                await asyncio.sleep(3600)
        return base(messages, info)
    fn.plans = True
    return fn


SCRIPTS["planner-hangs"] = hanging_planner()


async def _wait_for(cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if await cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_a_restarted_worker_resumes_the_plan_instead_of_starting_over(hq, planning_on):
    HANG["step2"] = 0
    a_worker = await hq.start_worker(name="A")
    agent = await hq.agent(name="Hi-Tech", model="fake:planner-hangs")
    t = await hq.thread(agent["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    r = hq.runtime.broker.r

    async def in_step_2():
        return HANG["step2"] >= 1
    await _wait_for(in_step_2)
    job = hq.jobs()[-1]
    await a_worker.stop(graceful=False)  # a deploy: the worker goes away mid-step
    plan = await nextactions.load(r, job.id)
    assert plan["status"] == "interrupted"
    assert [s["status"] for s in plan["steps"]] == ["done", "pending", "pending"]
    assert await r.ttl(f"ahq:plan:{job.id}") > settings.pad_ttl_hours * 3600  # not the finished expiry

    await hq.start_worker(name="B", claim_idle_ms=300)
    assert await hq.wait_agent_replies(t["id"], 1, timeout=20) == ["Your game is ready."]
    assert len(SEEN["planned"]) == 1  # planned once, not again after the restart
    assert [s["n"] for s in SEEN["steps"]].count(1) == 3  # step 1's three requests, once: not redone
    assert "This step was started before and interrupted" in SEEN["step2_prompts"][-1]
    plan = await nextactions.load(r, job.id)
    assert plan["status"] == "done" and plan["resumed"] == 1
    assert [s["status"] for s in plan["steps"]] == ["done", "done", "done"]
    assert any((m.meta or {}).get("kind") == "next_actions_resumed" and "at step 2 of 3" in m.content
               for m in hq.messages(t["id"]))
    assert hq.jobs()[-1].attempts == 2


async def test_stop_ends_the_plan(hq, planning_on):
    HANG["step2"] = 0
    await hq.start_worker()
    agent = await hq.agent(name="Hi-Tech", model="fake:planner-hangs")
    t = await hq.thread(agent["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")

    async def in_step_2():
        return HANG["step2"] >= 1
    await _wait_for(in_step_2)
    job = hq.jobs()[-1]
    assert (await hq.http.post(f"/api/threads/{t['id']}/stop")).status_code == 200
    r = hq.runtime.broker.r

    async def stopped():
        p = await nextactions.load(r, job.id)
        return p and p["status"] == "stopped"
    await _wait_for(stopped)
    plan = await nextactions.load(r, job.id)
    assert [s["status"] for s in plan["steps"]] == ["done", "stopped", "stopped"]
    assert 0 < await r.ttl(f"ahq:plan:{job.id}") <= settings.pad_ttl_hours * 3600


async def test_a_step_retries_when_its_tool_server_is_being_recreated(hq, planning_on, monkeypatch):
    from app import runtime as rt_mod

    monkeypatch.setattr(nextactions, "STEP_RETRY_WAIT", 0)
    w = await hq.start_worker()
    real = w.rt.run_agent
    calls = {"n": 0}

    async def flaky(agent, prompt, deps, **kw):
        if kw.get("step") and prompt.startswith("[Next actions: step 2"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise rt_mod.ToolServerUnreachable("workbench-1", "http://workbench-1:8000", "connection refused")
        return await real(agent, prompt, deps, **kw)
    monkeypatch.setattr(w.rt, "run_agent", flaky)
    agent = await hq.agent(name="Hi-Tech", model="fake:planner")
    t = await hq.thread(agent["id"])
    await hq.say(t["id"], "build me a zork inspired text based game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Your game is ready."]
    plan = await nextactions.load(hq.runtime.broker.r, hq.jobs()[-1].id)
    assert [s["status"] for s in plan["steps"]] == ["done", "done", "done"]
    assert plan["steps"][1]["restarts"] == 1 and plan["revisions"] == 0
    assert any((m.meta or {}).get("kind") == "step_retry" and "workbench-1" in m.content
               for m in hq.messages(t["id"]))



def test_the_reply_is_told_which_files_the_steps_already_sent():
    plan = {"steps": [{"n": 1, "title": "Build", "status": "done", "summary": "built", "details": ""}],
            "stopped_because": ""}
    text = nextactions.final_prompt("make a game", plan, worker=False,
                                    files=["tetris_game.tar.gz (att_x, 14 KB)"])
    assert "Files already attached during the steps (they go with your reply): tetris_game.tar.gz" in text
    assert "Send each finished file once" in text
    assert "Don't send files to the user from a step" in nextactions.STEP_RULES
