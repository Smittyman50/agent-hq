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
