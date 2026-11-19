"""A long coding run on a local model: trimming that scales with the window, telling a
cut-off tool call from a malformed one, and a fresh start for the fallback.

Regression (2026-11-19): Hi-Tech re-read a 600-line file chunk by chunk because only the
latest three tool results were kept whole, called a 750-token malformed tool call a
cut-off at its 16,384-token limit, and its fallback spent all 150 requests re-reading."""

from __future__ import annotations

from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.usage import RequestUsage

from app import runtime
from app.runtime import (RequestClock, handover_brief, server_error_cause, tool_result_limits,
                         trim_old_tool_results)

from .conftest import SCRIPTS, _last_prompt


def reads(n: int, size: int) -> list:
    msgs = [ModelRequest(parts=[UserPromptPart("fix the engine")])]
    for i in range(n):
        msgs.append(ModelResponse(parts=[ToolCallPart("read_file", {"path": "engine.py", "start_line": i * 100},
                                                      tool_call_id=f"c{i}")]))
        msgs.append(ModelRequest(parts=[ToolReturnPart("read_file", "x" * size, tool_call_id=f"c{i}")]))
    return msgs


def returns(msgs: list) -> list:
    return [p.content for m in msgs if isinstance(m, ModelRequest) for p in m.parts
            if isinstance(p, ToolReturnPart)]


def test_limits_scale_with_the_window():
    assert tool_result_limits(None) == (3, 40_000)  # unknown window: the old limits
    assert tool_result_limits(8192) == (3, 40_000)  # small windows never get less
    keep, over = tool_result_limits(32_768)
    assert keep == 8 and over > 40_000
    assert tool_result_limits(400_000)[0] == 12  # capped


def test_a_large_window_keeps_a_coding_runs_reads_whole():
    msgs = reads(8, 9000)  # 72,000 characters of file chunks
    assert all("shortened from" in c for c in returns(trim_old_tool_results(msgs))[:5])  # old rule
    assert trim_old_tool_results(msgs, window=400_000) == msgs
    mid = returns(trim_old_tool_results(msgs, window=32_768))
    assert all(c == "x" * 9000 for c in mid)  # 8 kept whole at 32k


def test_server_error_cause():
    assert server_error_cause(None, 16_384) == "unclear"
    assert server_error_cause(750, 16_384) == "malformed"
    assert server_error_cause(15_000, 16_384) == "cut_off"


def test_request_clock_estimates_what_the_failed_request_wrote(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: now[0])
    clock = RequestClock()
    msgs = [ModelRequest(parts=[UserPromptPart("go")])]
    clock.mark(msgs)
    assert clock.tokens_in_flight() is None  # no speed seen yet
    now[0] += 100  # a 2,500-token response took 100 s: 25 tokens a second
    msgs.append(ModelResponse(parts=[TextPart("...")], usage=RequestUsage(output_tokens=2500)))
    clock.mark(msgs)
    clock.mark(msgs)  # marked twice for one request: no bogus sample
    assert clock.rates == [25.0]
    now[0] += 30
    assert clock.tokens_in_flight() == 750


def test_handover_brief_names_files_and_tools():
    msgs = [ModelResponse(parts=[ToolCallPart("workbench-2_write_file", {"path": "world.py", "content": "x"})]),
            ModelResponse(parts=[ToolCallPart("workbench-2_write_file", {"path": "world.py", "content": "y"})]),
            ModelResponse(parts=[ToolCallPart("workbench-2_edit_file", {"path": "engine.py"}),
                                 TextPart("Now fixing the doors.")])]
    brief = handover_brief(msgs)
    assert "3 tool calls" in brief and "workbench-2_write_file ×2" in brief
    assert "world.py (2×)" in brief and "engine.py (1×)" in brief
    assert "Now fixing the doors." in brief


SEEN: dict = {}


def long_failer(messages, info: AgentInfo) -> ModelResponse:
    # Makes many steps, then the model server breaks.
    done = sum(1 for m in messages for p in getattr(m, "parts", []) if p.part_kind == "tool-return")
    if done > runtime.FRESH_START_AFTER_TOOL_CALLS:
        raise RuntimeError("llama-server returned invalid tool call arguments")
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": f"step-{done}"})])


def fresh_taker(messages, info: AgentInfo) -> ModelResponse:
    SEEN["kinds"] = [p.part_kind for m in messages for p in getattr(m, "parts", [])]
    SEEN["prompt"] = _last_prompt(messages)
    return ModelResponse(parts=[TextPart("finished from a fresh start")])


SCRIPTS.update({"long-failer": long_failer, "fresh-taker": fresh_taker})


async def test_fallback_starts_fresh_after_a_long_failed_run(hq):
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:long-failer", fallback_model="fake:fresh-taker",
                       delegates=[w["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    assert await hq.wait_agent_replies(t["id"], 1) == ["finished from a fresh start"]
    assert "tool-call" not in SEEN["kinds"]  # not the failed run's transcript
    assert SEEN["prompt"].startswith("build the game")
    assert "starting fresh" in SEEN["prompt"] and "job_status ×" in SEEN["prompt"]


def bad_json_writer(messages, info: AgentInfo) -> ModelResponse:
    # A short tool call the server can't parse; fine once told what went wrong.
    from pydantic_ai.exceptions import ModelHTTPError

    notes = [str(getattr(p, "content", "")) for m in messages for p in m.parts]
    SEEN["malformed_note"] = any("it wasn't cut off" in n for n in notes)
    SEEN["cut_off_note"] = any("write a big file in parts" in n for n in notes)
    if not (SEEN["malformed_note"] or SEEN["cut_off_note"]):
        raise ModelHTTPError(500, "qwen", {"error": "invalid tool call arguments"})
    return ModelResponse(parts=[TextPart("Summary: fixed the call.")])


SCRIPTS["bad-json-writer"] = bad_json_writer


async def test_a_short_failed_call_is_retried_as_malformed_not_cut_off(hq, monkeypatch):
    monkeypatch.setattr(RequestClock, "tokens_in_flight", lambda self: 750)
    await hq.start_worker()
    a = await hq.agent(name="Local", model="fake:bad-json-writer", fallback_model="fake:strong",
                       max_output_tokens=16384, thinking="off")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build it")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Summary: fixed the call."]
    assert SEEN["malformed_note"] and not SEEN["cut_off_note"]
    [event] = [m for m in hq.messages(t["id"]) if m.meta and m.meta.get("kind") == "cut_off_retry"]
    assert event.meta["cause"] == "malformed" and "malformed tool call" in event.content
