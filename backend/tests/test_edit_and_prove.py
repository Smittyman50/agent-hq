"""The workbench's edit_file tool, and the check that code changes get run before 'done'."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from app.runtime import _SAYS_UNTESTED, untested_changes

WB = Path(__file__).resolve().parents[2] / "workbench"


@pytest.fixture
def wb(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(WB))
    sys.modules.pop("workbench_mcp", None)
    mod = importlib.import_module("workbench_mcp")
    call = lambda name: getattr(getattr(mod, name), "fn", getattr(mod, name))  # noqa: E731
    return tmp_path, call


def test_edit_replaces_one_exact_match(wb):
    root, t = wb
    (root / "game.py").write_text("def look():\n    print('dark')\n\ndef go():\n    pass\n")
    out = t("edit_file")("game.py", "    print('dark')", "    print('A dim cave.')")
    assert "replaced 1 occurrence" in out and "A dim cave." in out
    assert (root / "game.py").read_text().count("A dim cave.") == 1


def test_edit_refuses_ambiguous_and_missing_text(wb):
    root, t = wb
    (root / "a.py").write_text("x = 1\nx = 1\n    def handle(cmd):\n")
    with pytest.raises(ValueError, match="occurs 2 times"):
        t("edit_file")("a.py", "x = 1", "x = 2")
    assert "replaced 2 occurrences" in t("edit_file")("a.py", "x = 1", "x = 2", replace_all=True)
    with pytest.raises(ValueError, match="closest text is around line 3"):
        t("edit_file")("a.py", "        def handle(cmd):", "x")  # indentation differs
    with pytest.raises(ValueError, match="doesn't exist"):
        t("edit_file")("missing.py", "a", "b")


def test_read_file_line_range(wb):
    root, t = wb
    (root / "big.py").write_text("\n".join(f"line {i}" for i in range(1, 101)))
    out = t("read_file")("big.py", start_line=10, end_line=12)
    assert out.splitlines() == ["   10  line 10", "   11  line 11", "   12  line 12", "[lines 10-12 of 100]"]


def _turn(*calls):
    msgs = [ModelRequest(parts=[UserPromptPart("build it")])]
    for k, (name, args) in enumerate(calls):
        msgs.append(ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"c{k}")]))
        msgs.append(ModelRequest(parts=[ToolReturnPart(name, "ok", tool_call_id=f"c{k}")]))
    return msgs


def test_changes_after_the_last_run_count_as_untested():
    assert untested_changes(_turn(("workbench-2_write_file", {"path": "game.py", "content": "x"}))) == ["game.py"]
    ran = _turn(("workbench-2_edit_file", {"path": "game.py", "old_text": "a", "new_text": "b"}),
                ("workbench-2_run_shell", {"command": "python -m unittest"}))
    assert untested_changes(ran) == []
    again = ran + _turn(("workbench-2_edit_file", {"path": "parser.py", "old_text": "a", "new_text": "b"}))[1:]
    assert untested_changes(again) == ["parser.py"]
    assert untested_changes(_turn(("workbench-2_write_file", {"path": "README.md", "content": "x"}))) == []
    # Only this turn counts: changes run in an earlier turn don't.
    earlier = _turn(("workbench-2_write_file", {"path": "game.py", "content": "x"}))
    assert untested_changes(earlier + [ModelRequest(parts=[UserPromptPart("thanks")])]) == []


def test_a_reply_may_say_it_could_not_test():
    assert _SAYS_UNTESTED.search("Tested: not testable here: needs the LAN")
    assert _SAYS_UNTESTED.search("I couldn't run it in the workbench.")
    assert not _SAYS_UNTESTED.search("Tested: python -m unittest, 4 passed")


def test_tool_traces_with_pydantic_arguments_are_json_safe():
    from pydantic import BaseModel

    from app.runtime import _json_safe

    class Planned(BaseModel):
        role: str
        est_minutes: int

    assert _json_safe([{"tool": "request_subagents", "args": {"subagents": [Planned(role="Feed reader", est_minutes=5)]}}]) == \
        [{"tool": "request_subagents", "args": {"subagents": [{"role": "Feed reader", "est_minutes": 5}]}}]
