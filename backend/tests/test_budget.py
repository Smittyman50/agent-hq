"""Running out of model requests: warn near the limit, then wrap up instead of an error;
and the workbench's edit_file taking several edits in one call.

Regression (2026-11-19): the fallback had a tested game 40 requests in, spent the other
110 on one small edit each, and at request 150 the turn ended as an error with no reply
and no files sent."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo

from app.runtime import BUDGET_MARK, budget_warning_at, with_budget_note

from .conftest import SCRIPTS

WB = Path(__file__).resolve().parents[2] / "workbench"


# -- the warning ------------------------------------------------------------------------

def test_warning_point_scales_with_the_limit():
    assert budget_warning_at(150) == 10
    assert budget_warning_at(50) == 5
    assert budget_warning_at(7) == 3


def notes(msgs) -> list[str]:
    return [p.content for m in msgs if isinstance(m, ModelRequest) for p in m.parts
            if isinstance(p, UserPromptPart) and p.content.startswith(BUDGET_MARK)]


def test_the_note_is_added_once_near_the_limit():
    msgs = [ModelRequest(parts=[UserPromptPart("build it")]),
            ModelResponse(parts=[ToolCallPart("read_file", {"path": "a.py"}, tool_call_id="c1")]),
            ModelRequest(parts=[ToolReturnPart("read_file", "x", tool_call_id="c1")])]
    assert with_budget_note(msgs, 100, 150) == msgs  # 50 left: nothing yet
    near = with_budget_note(msgs, 141, 150)
    assert notes(near) == [notes(near)[0]] and "9 more model requests" in notes(near)[0]
    assert isinstance(near[-1].parts[0], ToolReturnPart)  # added after the tool result
    assert with_budget_note(near, 145, 150) == near  # not again


# -- the wrap-up, end to end ------------------------------------------------------------

SEEN: dict = {}


def polisher(messages, info: AgentInfo) -> ModelResponse:
    # Keeps making small changes; when out of requests, sends nothing more and reports.
    said = [str(getattr(p, "content", "")) for m in messages for p in m.parts if p.part_kind == "user-prompt"]
    SEEN["warned"] = any(s.startswith(BUDGET_MARK) for s in said)
    if any("used this run's model requests" in s for s in said):
        return ModelResponse(parts=[TextPart("Report: the game runs and its tests pass; puzzles still rough.")])
    return ModelResponse(parts=[ToolCallPart("job_status", {"job_id": "polish"})])


def broken(messages, info: AgentInfo) -> ModelResponse:
    raise RuntimeError("model server down")


SCRIPTS.update({"polisher": polisher, "broken-main": broken})


async def wrap_up_events(hq, thread_id):
    return [m for m in hq.messages(thread_id) if (m.meta or {}).get("kind") == "wrap_up"]


async def test_an_agent_without_a_fallback_wraps_up_at_its_limit(hq):
    SEEN.clear()
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Solo", model="fake:polisher", delegates=[w["id"]], max_requests=12)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == [
        "Report: the game runs and its tests pass; puzzles still rough."]
    assert SEEN["warned"]
    [event] = await wrap_up_events(hq, t["id"])
    assert "all 12 of its model requests" in event.content
    assert not [m for m in hq.messages(t["id"]) if m.content.startswith("Error:")]


async def test_the_fallback_wraps_up_at_its_limit(hq):
    SEEN.clear()
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:broken-main", fallback_model="fake:polisher",
                       delegates=[w["id"]], max_requests=12)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "build the game")
    [reply] = await hq.wait_agent_replies(t["id"], 1, timeout=30)
    assert reply.startswith("Report:") and SEEN["warned"]
    assert len(await wrap_up_events(hq, t["id"])) == 1


async def test_a_main_model_with_a_fallback_still_hands_over_at_its_limit(hq):
    # A looping local model should go to the fallback, not wrap up (and isn't warned).
    SEEN.clear()
    await hq.start_worker()
    w = await hq.agent(name="Helper", description="x", model="fake:worker")
    a = await hq.agent(name="Local", model="fake:polisher", fallback_model="fake:strong",
                       delegates=[w["id"]], max_requests=12)
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1, timeout=30) == ["answered by the fallback model"]
    assert not await wrap_up_events(hq, t["id"])


SCRIPTS.setdefault("strong", lambda m, i: ModelResponse(parts=[TextPart("answered by the fallback model")]))


# -- several edits in one edit_file call ------------------------------------------------

@pytest.fixture
def wb(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(WB))
    sys.modules.pop("workbench_mcp", None)
    mod = importlib.import_module("workbench_mcp")
    call = lambda name: getattr(getattr(mod, name), "fn", getattr(mod, name))  # noqa: E731
    return tmp_path, call


GAME = "def look():\n    print('dark')\n\ndef go():\n    pass\n\nSCORE = 0\n"


def test_several_edits_apply_in_order(wb):
    root, t = wb
    (root / "game.py").write_text(GAME)
    out = t("edit_file")("game.py", edits=[
        {"old_text": "    print('dark')", "new_text": "    print('A dim cave.')"},
        {"old_text": "    pass", "new_text": "    print('You go.')"},
        {"old_text": "A dim cave.", "new_text": "A dim, dripping cave."},  # sees the first edit
    ])
    assert out.startswith("applied 3 edits to game.py")
    assert (root / "game.py").read_text() == GAME.replace("print('dark')", "print('A dim, dripping cave.')") \
        .replace("    pass", "    print('You go.')")


def test_one_bad_edit_changes_nothing(wb):
    root, t = wb
    (root / "game.py").write_text(GAME)
    with pytest.raises(ValueError, match=r"(?s)edit 2 of 2: old_text isn.t in game.py.*Nothing was changed"):
        t("edit_file")("game.py", edits=[
            {"old_text": "SCORE = 0", "new_text": "SCORE = 10"},
            {"old_text": "def run():", "new_text": "def play():"},
        ])
    assert (root / "game.py").read_text() == GAME


def test_edits_and_old_text_dont_mix(wb):
    root, t = wb
    (root / "game.py").write_text(GAME)
    with pytest.raises(ValueError, match="not both"):
        t("edit_file")("game.py", "SCORE = 0", "SCORE = 1", edits=[{"old_text": "a", "new_text": "b"}])
    with pytest.raises(ValueError, match="empty"):
        t("edit_file")("game.py", edits=[])
    # The single edit still answers as before.
    assert t("edit_file")("game.py", "SCORE = 0", "SCORE = 1").startswith("replaced 1 occurrence in game.py")
