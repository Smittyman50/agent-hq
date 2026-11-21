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
    assert limit_settings(a) == {"max_tokens": 16384, "max_retries": 0, "request_timeout": 300.0}
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


def big_writer(messages, info: AgentInfo) -> ModelResponse:
    # Writes a whole file in one call and gets cut off (Ollama: a 500); in parts once told.
    from pydantic_ai.exceptions import ModelHTTPError

    STATE["calls"] += 1
    told = any("write a big file in parts" in str(getattr(p, "content", ""))
               for m in messages for p in m.parts)
    STATE["told"] = told
    if not told:
        raise ModelHTTPError(500, "qwen", {"error": "failed to parse tool call"})
    return ModelResponse(parts=[TextPart("Summary: wrote it in parts.")])


SCRIPTS.update({"overthinker": overthinker, "big-writer": big_writer})


async def test_overthinking_retries_on_the_same_model_before_the_fallback(hq):
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:overthinker", fallback_model="fake:strong",
                       max_output_tokens=4096)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build it")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Summary: did it with less thinking."]
    kinds = [m.meta.get("kind") for m in hq.messages(t["id"]) if m.meta]
    assert "think_less" in kinds and "model_fallback" not in kinds


async def test_a_server_error_retries_once_asking_for_smaller_tool_calls(hq):
    # Regression (2026-11-18): three 5-minute 500s in one run, each a write_file cut off at
    # the 8,192-token limit and quietly re-sent by the SDK.
    STATE.update(calls=0, told=False)
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:big-writer", fallback_model="fake:strong",
                       max_output_tokens=8192, thinking="off")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build it")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Summary: wrote it in parts."]
    kinds = [m.meta.get("kind") for m in hq.messages(t["id"]) if m.meta]
    assert "cut_off_retry" in kinds and "model_fallback" not in kinds
    assert STATE["told"]


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


# -- links to files the turn already sent --------------------------------------------------

def _sent_turn():
    from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

    return [ModelRequest(parts=[UserPromptPart("build the game")]),
            ModelResponse(parts=[ToolCallPart("send_from_workbench",
                                              {"path": "/work/deepdelving-game.zip"}, tool_call_id="s1")]),
            ModelRequest(parts=[ToolReturnPart("send_from_workbench",
                                               {"attached": True, "attachment": "att_d4a0f9172c9f",
                                                "name": "deepdelving-game.zip"}, tool_call_id="s1")])]


def test_a_link_to_a_file_already_sent_says_not_to_send_it_again():
    # Regression (2026-11-18): the fallback sent the zip, linked to it as attachment:att_...,
    # was told to send it, and sent it a second time.
    from app.runtime import file_link_nudge

    for link in ("attachment:att_d4a0f9172c9f", "sandbox:/work/deepdelving-game.zip", "/work/deepdelving-game.zip"):
        nudge = file_link_nudge(f"**Files:** [the game]({link})", _sent_turn())
        assert nudge and "don't send them again" in nudge, link


def test_a_link_to_a_file_never_sent_still_asks_for_it():
    from app.runtime import file_link_nudge

    nudge = file_link_nudge("[tests](sandbox:/work/test_game.py)", _sent_turn())
    assert "Send each one with send_from_workbench" in nudge and "test_game.py" in nudge
    assert file_link_nudge("Sent deepdelving-game.zip.", _sent_turn()) is None


# -- model requests per run ------------------------------------------------------------------

def tool_looper(messages, info: AgentInfo) -> ModelResponse:
    # A model that keeps calling tools (here: a job that isn't there).
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": "nope"})])


SCRIPTS.update({"tool-looper": tool_looper})
# test_fallback.py defines this too; set it here as well so this file runs on its own.
SCRIPTS.setdefault("strong", lambda m, i: ModelResponse(parts=[TextPart("answered by the fallback model")]))


async def test_an_agents_request_limit_replaces_the_default(hq):
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:tool-looper", fallback_model="fake:strong",
                       delegates=[w["id"]], max_requests=7)
    assert a["max_requests"] == 7
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == ["answered by the fallback model"]
    switched = [m.content for m in hq.messages(t["id"]) if (m.meta or {}).get("kind") == "model_fallback"]
    assert switched and "request_limit of 7" in switched[0]


def test_truncated_nudge_asks_for_the_whole_answer_again():
    # Regression (2026-11-21): a delegate cut off mid-report kept the cut-off text in view,
    # wrote only the missing tail on the retry, and its first findings never reached anyone.
    assert "thrown away" in TRUNCATED_NUDGE
    assert "from the beginning" in TRUNCATED_NUDGE
    assert "don't continue where it stopped" in TRUNCATED_NUDGE
