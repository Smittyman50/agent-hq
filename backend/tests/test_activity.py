"""Live activity (app/activity.py, GET /api/activity): model requests, tokens and tool
calls are counted as they happen, not when the run ends, and a running job shows what its
agent is doing right now.

Why: the usage ledger gets one row per run, at the end. A goal turn on a local model ran
for close to two hours (2026-11-23, Hi-Tech building the Zork-style game: 620 model
requests, 704 tool calls, 20M prompt tokens) with nothing to show for it until it ended."""

from __future__ import annotations

import asyncio

from pydantic_ai import ModelResponse, TextPart, ToolCallPart

from app import activity

from .conftest import SCRIPTS, _tool_returns

GATE: dict[str, asyncio.Event] = {}


async def plodder(messages, info):
    """Looks up skills twice, waits at the gate, then answers."""
    n = len([r for r in _tool_returns(messages) if r.tool_name == "list_skills"])
    if n < 2:
        return ModelResponse(parts=[ToolCallPart("list_skills", {"query": f"pass {n + 1}"})])
    GATE["reached"].set()
    await GATE["open"].wait()
    return ModelResponse(parts=[TextPart("All done")])


SCRIPTS["plodder"] = plodder


async def _wait(cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if await cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


async def test_counts_as_it_happens_and_shows_the_running_job(hq):
    GATE.update(reached=asyncio.Event(), open=asyncio.Event())
    await hq.start_worker()
    a = await hq.agent(name="Plodder", model="fake:plodder")
    idle = await hq.agent(name="Idler", model="fake:echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "look around")
    await asyncio.wait_for(GATE["reached"].wait(), 10)

    # Mid-run: the requests and tool calls so far are already counted.
    snap = (await hq.http.get("/api/activity", params={"window": 15})).json()
    me = next(x for x in snap["agents"] if x["agent_id"] == a["id"])
    assert me["name"] == "Plodder"
    assert me["totals"]["requests"] == 2 and me["totals"]["tools"] == 2
    assert me["totals"]["input"] > 0 and me["totals"]["output"] > 0
    assert sum(me["series"]["tools"]) == 2 and len(me["series"]["tools"]) == 15
    assert me["top_tools"] == [{"tool": "list_skills", "calls": 2}]
    # The job is running, waiting on its third model request.
    job = next(j for j in snap["jobs"] if j["agent_id"] == a["id"])
    assert job["state"] == "model" and job["kind"] == "turn" and job["thread_id"] == t["id"]
    assert job["requests"] == 2 and job["tools"] == 2
    # Idle agents are listed too, after the busy one.
    names = [x["name"] for x in snap["agents"]]
    assert names.index("Plodder") < names.index("Idler")
    assert next(x for x in snap["agents"] if x["agent_id"] == idle["id"])["totals"]["requests"] == 0
    kinds = [e["type"] for e in snap["feed"]]
    assert "job_started" in kinds and kinds.count("tool") == 2

    GATE["open"].set()
    assert (await hq.wait_agent_replies(t["id"], 1))[0] == "All done"

    async def finished():
        s = (await hq.http.get("/api/activity")).json()
        return not [j for j in s["jobs"] if j["agent_id"] == a["id"]]

    await _wait(finished)
    snap = (await hq.http.get("/api/activity", params={"window": 1440})).json()
    me = next(x for x in snap["agents"] if x["agent_id"] == a["id"])
    assert me["totals"]["requests"] >= 3 and me["totals"]["runs"] >= 1
    assert snap["bucket_s"] == 3600 and len(snap["buckets"]) == 24
    assert snap["feed"][0]["type"] in ("job_finished", "model")


async def test_a_failed_tool_counts_as_an_error(hq):
    r = hq.broker.r
    started = await activity.tool_started(r, agent_id="a1", job_id=None, tool="run_shell")
    await activity.tool_finished(r, agent_id="a1", agent="A", job_id=None, tool="run_shell",
                                 started=started, error="boom")
    snap = await activity.snapshot(r, 15)
    a = snap["agents"][0]
    assert a["totals"]["tools"] == 1 and a["totals"]["tool_errors"] == 1
    assert snap["feed"][0]["error"] == "boom"


async def test_a_stray_update_after_the_job_ended_leaves_no_record(hq):
    r = hq.broker.r
    await activity._set_state(r, "gone", state="model", since="x", detail="m")
    assert not await r.exists("ahq:act:job:gone")
