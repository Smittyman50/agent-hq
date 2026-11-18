"""Per-agent guardrails for the main model: an output-token limit and a request time limit."""

from __future__ import annotations

import asyncio

from pydantic_ai import Agent, ModelRetry
from pydantic_ai.exceptions import (IncompleteToolCall, ModelRequestTimeout, OutputTokenLimitExceeded,
                                    UnexpectedModelBehavior)
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.models import AgentDef
from app.runtime import MAX_CONTINUE_NUDGES, TRUNCATED_NUDGE, limit_settings, ran_out

from .conftest import SCRIPTS


def test_limits_become_model_settings_for_the_main_model_only():
    a = AgentDef(name="x", max_output_tokens=16384, request_timeout_s=300, fallback_model="fake:strong")
    assert limit_settings(a) == {"max_tokens": 16384, "request_timeout": 300.0}
    assert limit_settings(a, on_fallback=True) == {}
    assert limit_settings(AgentDef(name="y")) == {}


def test_what_counts_as_running_out():
    assert ran_out(OutputTokenLimitExceeded("limit")) == "it used up its output limit before answering"
    assert ran_out(ModelRequestTimeout("qwen", 300)) == "its request ran past the time limit"
    assert ran_out(IncompleteToolCall("cut mid-call")) is None  # less thinking won't help
    assert ran_out(RuntimeError("500")) is None
    try:  # wrapped, as runs raise them
        try:
            raise OutputTokenLimitExceeded("limit")
        except OutputTokenLimitExceeded as inner:
            raise RuntimeError("run failed") from inner
    except RuntimeError as e:
        assert ran_out(e) is not None


def test_thinking_only_response_at_the_limit_raises():
    # What a model that spends its whole budget thinking looks like to PydanticAI.
    a = Agent(FunctionModel(lambda m, i: ModelResponse(parts=[ThinkingPart("hmm " * 50)],
                                                        finish_reason="length")))
    try:
        asyncio.run(a.run("go"))
    except OutputTokenLimitExceeded as e:
        assert ran_out(e)
    else:
        raise AssertionError("expected OutputTokenLimitExceeded")


def test_cut_off_text_is_sent_back():
    seen = []

    def model(messages, info: AgentInfo) -> ModelResponse:
        texts = [getattr(p, "content", "") for m in messages for p in m.parts]
        seen.append(any(isinstance(t, str) and "reply was cut off" in t for t in texts))
        if seen[-1]:
            return ModelResponse(parts=[TextPart("Done: short answer.")])
        return ModelResponse(parts=[TextPart("Here is the full design of the gam")], finish_reason="length")

    a = Agent(FunctionModel(model), retries=3)

    @a.output_validator
    def check(ctx, out):  # the same rule runtime.build_agent installs
        last = ctx.messages[-1]
        if isinstance(out, str) and ctx.retry < MAX_CONTINUE_NUDGES and last.finish_reason == "length":
            raise ModelRetry(TRUNCATED_NUDGE)
        return out

    assert asyncio.run(a.run("go")).output == "Done: short answer."
    assert seen == [False, True]


# -- end to end (needs Valkey, like the other worker tests) ------------------------------

STATE = {"calls": 0, "thinking": []}


def overthinker(messages, info: AgentInfo) -> ModelResponse:
    # Thinks past its limit on the first try; acts once it's asked to think less.
    STATE["calls"] += 1
    if STATE["calls"] == 1:
        return ModelResponse(parts=[ThinkingPart("let me consider " * 100)], finish_reason="length")
    return ModelResponse(parts=[TextPart("Summary: did it with less thinking.")])


SCRIPTS.update({"overthinker": overthinker})


async def test_overthinking_retries_on_the_same_model_before_the_fallback(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:overthinker", fallback_model="fake:strong",
                       max_output_tokens=4096)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build it")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Summary: did it with less thinking."]
    kinds = [m.meta.get("kind") for m in hq.messages(t["id"]) if m.meta]
    assert "think_less" in kinds and "model_fallback" not in kinds


def test_less_thinking_means_off_for_on_off_ollama_models(monkeypatch):
    from app import thinking
    from app.models import ModelProvider, ProviderKind

    async def thinks(p, m):
        return True

    monkeypatch.setattr(thinking, "ollama_thinks", thinks)
    ollama = ModelProvider(name="o", kind=ProviderKind.ollama)
    monkeypatch.setattr(thinking, "_provider", lambda pid: ollama)
    qwen = AgentDef(name="q", provider_id="p", model="qwen3.8:27b-q3")
    oss = AgentDef(name="g", provider_id="p", model="gpt-oss:20b")
    assert asyncio.run(thinking.settings_for(qwen, override="less")) == {"extra_body": {"reasoning_effort": "none"}}
    assert asyncio.run(thinking.settings_for(oss, override="less")) == {"extra_body": {"reasoning_effort": "low"}}
