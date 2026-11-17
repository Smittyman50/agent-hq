"""Fallback models: the main (local) model fails or escalates, the fallback takes over."""

from __future__ import annotations

from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from app.models import Role

from .conftest import SCRIPTS, _last_prompt

SEEN = {"fallback_tools": set(), "fallback_prompt": ""}


def escalator(messages, info: AgentInfo) -> ModelResponse:
    assert "escalate" in {t.name for t in info.function_tools}
    return ModelResponse(parts=[ToolCallPart("escalate", {"reason": "this needs deeper reasoning"})])


def strong(messages, info: AgentInfo) -> ModelResponse:
    SEEN["fallback_tools"] = {t.name for t in info.function_tools}
    SEEN["fallback_prompt"] = _last_prompt(messages)
    return ModelResponse(parts=[TextPart("answered by the fallback model")])


def looper(messages, info: AgentInfo) -> ModelResponse:
    # A confused local model that never stops calling tools.
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": "nope"})])


SCRIPTS.update({"escalator": escalator, "strong": strong, "looper": looper})


async def test_model_error_falls_back(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:broken", fallback_model="fake:strong")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    assert await hq.wait_agent_replies(t["id"], 1) == ["answered by the fallback model"]
    events = [m for m in hq.messages(t["id"]) if m.role == Role.event]
    assert events[0].meta["kind"] == "model_fallback"
    assert "model server unreachable" in events[0].content
    assert "escalate" not in SEEN["fallback_tools"]  # the fallback cannot escalate again


async def test_agent_can_escalate_itself(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:escalator", fallback_model="fake:strong")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "prove the Riemann hypothesis")
    assert await hq.wait_agent_replies(t["id"], 1) == ["answered by the fallback model"]
    assert "escalated: this needs deeper reasoning" in SEEN["fallback_prompt"]


async def test_runaway_tool_loop_falls_back(hq):
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:looper", fallback_model="fake:strong",
                       delegates=[w["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == ["answered by the fallback model"]
    assert "UsageLimitExceeded" in hq.messages(t["id"])[1].content


async def test_no_fallback_means_the_error_is_reported(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:broken")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    import asyncio
    for _ in range(100):
        events = [m.content for m in hq.messages(t["id"]) if m.role == Role.event]
        if events:
            break
        await asyncio.sleep(0.05)
    assert events[0].startswith("Error:")


async def test_unreachable_tool_server_is_named_and_skips_fallback(hq):
    # A tool server that is down (here: nothing listens on port 9) can't be fixed by another
    # model, so the fallback isn't tried and the error says which server and where.
    await hq.start_worker()
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": "deadbrowser", "transport": "http", "url": "http://127.0.0.1:9/mcp"})).json()
    a = await hq.agent(name="Local", model="fake:strong", fallback_model="fake:strong",
                       tool_server_ids=[srv["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    import asyncio
    for _ in range(200):
        events = [m for m in hq.messages(t["id"]) if m.role == Role.event]
        if events:
            break
        await asyncio.sleep(0.05)
    assert [e.meta.get("kind") for e in events] == ["error"]  # no model_fallback first
    assert "Can't reach tool server deadbrowser (http://127.0.0.1:9/mcp)" in events[0].content


def half_done(messages, info: AgentInfo) -> ModelResponse:
    # Does one step of the work, then the model server breaks on the next request.
    if any(p.part_kind == "tool-return" for m in messages for p in getattr(m, "parts", [])):
        raise RuntimeError("llama-server returned invalid tool call arguments")
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": "step-one"})])


def resumer(messages, info: AgentInfo) -> ModelResponse:
    SEEN["resumed_with"] = [p.part_kind for m in messages for p in getattr(m, "parts", [])]
    SEEN["fallback_prompt"] = _last_prompt(messages)
    return ModelResponse(parts=[TextPart("finished what the main model started")])


def narrator(messages, info: AgentInfo) -> ModelResponse:
    # Announces the work and stops, as small local models do, until it's told to carry on.
    parts = [p for m in messages for p in getattr(m, "parts", [])]
    # The nudge reaches the model as feedback text; match it by its wording.
    nudges = sum(1 for p in parts if isinstance(getattr(p, "content", None), str)
                 and "made no tool call" in p.content)
    SEEN["nudges"] = nudges
    if any(p.part_kind == "tool-return" for p in parts):
        return ModelResponse(parts=[TextPart("Summary: built it and ran it.")])
    if nudges == 0:
        return ModelResponse(parts=[TextPart("This is a big build.\n\nI'll now write the world files.")])
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": "x"})])


def stubborn(messages, info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("I'll get to it shortly.")])


SCRIPTS.update({"half_done": half_done, "resumer": resumer, "narrator": narrator,
                "stubborn": stubborn})


async def test_fallback_continues_from_the_main_models_progress(hq):
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:half_done", fallback_model="fake:resumer",
                       delegates=[w["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["finished what the main model started"]
    kinds = SEEN["resumed_with"]
    # The fallback saw the request, the main model's tool call and its result, then the note.
    assert kinds.index("tool-call") < kinds.index("tool-return") < len(kinds) - 1
    assert kinds.count("user-prompt") >= 2
    assert "Take over from where it stopped" in SEEN["fallback_prompt"]


async def test_reply_that_only_announces_work_is_sent_back(hq):
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:narrator", delegates=[w["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Summary: built it and ran it."]
    assert SEEN["nudges"] == 1


async def test_nudges_are_capped(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:stubborn")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["I'll get to it shortly."]


def test_announcement_detection():
    from app.runtime import announces_unstarted_work as a
    assert a("I'm starting with the world definition, then items. I'll run it and test it.")
    assert a("I'll now build the complete adventure. This will take a while.")
    assert not a("Which would you prefer: compact, or a larger game?")
    assert not a("Summary: done.\n\nIf you want, I'll add a save command.")
    assert not a("Done. Let me know if you want more rooms.")
    assert not a("All tests pass.")
