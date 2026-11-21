"""Scratch pads: notes kept outside the context window, in Valkey only (app/scratchpad.py)."""

from __future__ import annotations

from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from app import scratchpad
from app.config import settings

from .conftest import SCRIPTS, _instructions, _tool_returns

SEEN: dict[str, list] = {}


def researcher(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Notes two findings, reads them back, then answers from them."""
    SEEN.setdefault("instructions", []).append(_instructions(messages))
    returns = _tool_returns(messages)
    done = [r.tool_name for r in returns]
    if done.count("note") < 2:
        n = done.count("note") + 1
        return ModelResponse(parts=[ToolCallPart("note", {
            "text": f"Finding {n}: source https://example.com/{n}"})])
    if "read_notes" not in done:
        return ModelResponse(parts=[ToolCallPart("read_notes", {})])
    pad = next(r.content for r in returns if r.tool_name == "read_notes")
    SEEN.setdefault("pad", []).append(pad)
    summary = f"Checked: {pad['count']} findings"
    if info.output_tools:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
            "status": "done", "summary": summary, "details": ""})])
    return ModelResponse(parts=[TextPart(summary)])


def reader_boss(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Delegates to Researcher, then reads the researcher's notes by job ID."""
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("delegate", {
            "agent_name": "Researcher", "task": "look it up"})])
    by = {r.tool_name: r.content for r in returns}
    if "read_notes" not in by:
        SEEN["result"] = by["delegate"]
        job_id = by["delegate"]["notes"].split('job_id="')[1].split('"')[0]
        return ModelResponse(parts=[ToolCallPart("read_notes", {"job_id": job_id})])
    SEEN["their_notes"] = by["read_notes"]
    return ModelResponse(parts=[TextPart("Done")])


SCRIPTS.update({"researcher": researcher, "reader-boss": reader_boss})


async def _pad_keys(hq) -> list[str]:
    return sorted([k async for k in hq.runtime.broker.r.scan_iter("ahq:pad*")])


async def test_notes_are_kept_and_read_back(hq):
    await hq.start_worker()
    SEEN.clear()
    a = await hq.agent(name="Researcher", model="fake:researcher")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "check the claims")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Checked: 2 findings"]
    pad = SEEN["pad"][0]
    assert pad["count"] == 2
    assert pad["notes"] == ("1. Finding 1: source https://example.com/1\n"
                            "2. Finding 2: source https://example.com/2")
    job = hq.jobs()[-1]
    r = hq.runtime.broker.r
    # Kept in Valkey only, with the after-the-job expiry, not the running one.
    ttl = await r.ttl(f"ahq:pad:{job.id}")
    assert 0 < ttl <= settings.pad_ttl_hours * 3600
    # Every agent gets the rules.
    assert "You have a scratch pad for this job" in SEEN["instructions"][0]


async def test_delegator_reads_the_delegates_notes_and_can_keep_them(hq):
    await hq.start_worker(concurrency=4)
    SEEN.clear()
    w = await hq.agent(name="Researcher", model="fake:researcher")
    b = await hq.agent(name="Lead", model="fake:reader-boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1) == ["Done"]
    assert "kept 2 notes" in SEEN["result"]["notes"]
    theirs = SEEN["their_notes"]
    assert theirs["by"] == "Researcher" and "Finding 2" in theirs["notes"]

    task = next(j for j in hq.jobs() if j.kind == "task")
    r = await hq.http.get(f"/api/jobs/{task.id}/notes")
    body = r.json()
    assert body["count"] == 2 and [n["n"] for n in body["notes"]] == [1, 2]
    assert body["expires_in"] and not body["gone"]

    # Keep as file: a Markdown file on the delegation card.
    r = await hq.http.post(f"/api/jobs/{task.id}/notes/keep")
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["name"] == f"researcher-notes-{task.id}.md"
    card = next(m for m in hq.messages(t["id"]) if (m.meta or {}).get("job_id") == task.id
                and m.meta.get("kind") == "delegation_result")
    assert [x["id"] for x in card.meta["attachments"]] == [f["id"]]
    text = (await hq.http.get(f"/api/attachments/{f['id']}")).content.decode()
    assert text.startswith("# Researcher's notes") and "Task: look it up" in text


async def test_notes_from_another_conversation_are_refused(hq):
    await hq.start_worker()
    SEEN.clear()
    a = await hq.agent(name="Researcher", model="fake:researcher")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "check")
    await hq.wait_agent_replies(t["id"], 1)
    job = hq.jobs()[-1]
    r = hq.runtime.broker.r
    assert (await scratchpad.info(r, job.id))["thread"] == t["id"]
    # A missing pad reads as gone, not an error.
    body = (await hq.http.get(f"/api/jobs/{job.id}/notes")).json()
    assert body["count"] == 2
    await r.delete(f"ahq:pad:{job.id}", f"ahq:padinfo:{job.id}")
    body = (await hq.http.get(f"/api/jobs/{job.id}/notes")).json()
    assert body["gone"] and body["notes"] == []
    assert (await hq.http.post(f"/api/jobs/{job.id}/notes/keep")).status_code == 410


async def test_a_full_pad_says_so(hq, monkeypatch):
    monkeypatch.setattr(settings, "pad_max_bytes", 40)
    r = hq.runtime.broker.r
    await scratchpad.add(r, "j1", "x" * 30, thread_id="t", agent_id="a")
    try:
        await scratchpad.add(r, "j1", "y" * 30, thread_id="t", agent_id="a")
        raise AssertionError("expected PadFull")
    except scratchpad.PadFull as e:
        assert "read_notes" in str(e)
    assert (await scratchpad.info(r, "j1"))["count"] == 1


async def test_pages(hq, monkeypatch):
    monkeypatch.setattr(scratchpad, "PAGE_CHARS", 50)
    r = hq.runtime.broker.r
    for i in range(5):
        await scratchpad.add(r, "j2", f"note number {i} with some text", thread_id="t", agent_id="a")
    p1 = await scratchpad.page(r, "j2")
    assert p1["count"] == 5 and p1["next_offset"] < 5
    p2 = await scratchpad.page(r, "j2", p1["next_offset"])
    assert p2["notes"].startswith(f"{p1['next_offset'] + 1}. ")
