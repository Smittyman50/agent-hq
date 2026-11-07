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
