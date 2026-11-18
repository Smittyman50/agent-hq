"""Old tool calls with huge arguments (whole files sent to write_file) get shortened."""

from __future__ import annotations

import json

from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart

from app.context import history_tokens, make_trimmer
from app.runtime import shorten_old_tool_calls


def _rewrites(n: int, size: int = 19_000, as_json: bool = False):
    msgs = [ModelRequest(parts=[UserPromptPart("build the game")])]
    for k in range(n):
        args = {"path": "game.py", "content": "x" * size}
        msgs.append(ModelResponse(parts=[ToolCallPart("write_file", json.dumps(args) if as_json else args,
                                                      tool_call_id=f"c{k}")]))
        msgs.append(ModelRequest(parts=[ToolReturnPart("write_file", f"wrote {size}", tool_call_id=f"c{k}")]))
    return msgs


def test_old_calls_are_shortened_and_the_latest_kept():
    msgs = _rewrites(6, as_json=True)
    out = shorten_old_tool_calls(msgs, keep=3)
    calls = [p for m in out if isinstance(m, ModelResponse) for p in m.parts]
    assert all(len(c.args["content"]) < 400 and "omitted" in c.args["content"] for c in calls[:3])
    assert all(len(json.loads(c.args)["content"]) == 19_000 for c in calls[3:])
    assert all(c.args["path"] == "game.py" for c in calls[:3])  # short args untouched
    assert out[0] is msgs[0]  # the user's request stays


def test_trimmer_fits_a_32k_window_without_dropping_the_request():
    msgs = _rewrites(6)
    assert history_tokens(msgs) > 32_000
    out = make_trimmer(20_000)(msgs)
    assert history_tokens(out) <= 20_000
    assert isinstance(out[0].parts[0], UserPromptPart)
    assert len(out[-2].parts[0].args["content"]) == 19_000  # the newest call is whole


# -- calibrating the estimate against what the model reports --------------------------------

def _run_of(n: int, real_per_estimate: float, result_chars: int = 3000):
    """A run of n tool calls whose responses report real prompt sizes real_per_estimate
    times this module's estimate (Qwen with 27 tools: about 1.3)."""
    from pydantic_ai.usage import RequestUsage

    from app.context import history_tokens

    msgs = [ModelRequest(parts=[UserPromptPart("build the game")])]
    for k in range(n):
        est = 2000 + history_tokens(msgs)  # 2000: the instructions
        msgs.append(ModelResponse(parts=[ToolCallPart("run_shell", {"command": f"step {k}"}, tool_call_id=f"c{k}")],
                                  usage=RequestUsage(input_tokens=int(est * real_per_estimate))))
        msgs.append(ModelRequest(parts=[ToolReturnPart("run_shell", "x" * result_chars, tool_call_id=f"c{k}")]))
    return msgs


def test_calibration_reads_the_models_own_counts():
    from app.context import calibration

    assert calibration([ModelRequest(parts=[UserPromptPart("hi")])], 2000) == 1.0  # nothing to go on yet
    assert 1.25 < calibration(_run_of(5, 1.3), 2000) < 1.35
    assert calibration(_run_of(5, 0.8), 2000) == 1.0  # never trims less than the estimate says
    assert calibration(_run_of(5, 5.0), 2000) == 2.0  # capped


def test_trimmer_keeps_the_real_prompt_inside_the_window():
    # Regression (2026-11-18): the estimate said 24k while qwen received 31.9k (3.0 characters
    # a token on code and JSON, plus 3.6k tokens of tool definitions), the next request
    # overflowed the 32k window and the tool call came back cut off.
    from app.context import calibration, history_tokens, make_trimmer

    fixed, window = 2000, 32768
    budget = int(window * 0.9) - fixed - 4096
    # 70 results just under the size the ordinary trim shortens: about 23k tokens by the
    # estimate, under budget by the estimate, about 30k for the model.
    msgs = _run_of(70, 1.3, result_chars=1100)
    assert budget * 0.85 < history_tokens(msgs) <= budget
    out = make_trimmer(budget, fixed_tokens=fixed)(msgs)
    real_next = calibration(msgs, fixed) * (fixed + history_tokens(out))
    assert real_next <= window * 0.9 - 4096 + 1
    # Uncorrected, the trimmer would have sent it all, and that overflows for real.
    assert calibration(msgs, fixed) * (fixed + history_tokens(msgs)) > window * 0.9 - 4096
    assert history_tokens(out) < history_tokens(msgs)
