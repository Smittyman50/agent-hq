"""Tool approvals, end to end, with an agent calling a real workbench over MCP."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from sqlmodel import select

from app import db
from app.models import ToolApproval

WB_DIR = Path(__file__).resolve().parents[2] / "workbench"


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    return tmp_path_factory.mktemp("bench")


async def _setup(hq, workdir, model="fake:coder", rules=None):
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.WORK.mkdir(parents=True, exist_ok=True); w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    body = {"name": "bench", "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
            "env": {"WORKBENCH_DIR": str(workdir)}}
    if rules is not None:
        body["approval_rules"] = rules
    srv = (await hq.http.post("/api/tool-servers", json=body)).json()
    a = await hq.agent(name="Flint", model=model, tool_server_ids=[srv["id"]])
    return srv, a, await hq.thread(a["id"])


async def _pending(hq, n=1, timeout=20):
    for _ in range(timeout * 20):
        rows = (await hq.http.get("/api/tool-approvals?status=pending")).json()
        if len(rows) >= n:
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError("no approval request appeared")


async def test_probe_shows_the_servers_suggested_approvals(hq, workdir):
    srv, _, _ = await _setup(hq, workdir)
    tools = {t["name"]: t for t in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert tools["run_python"]["suggested_approval"] == "ask" and tools["run_python"]["approval"] == "ask"
    assert tools["read_file"]["approval"] == "allow"
    await hq.http.put(f"/api/tool-servers/{srv['id']}", json={**srv, "approval_rules": {"run_python": "allow"}})
    tools = {t["name"]: t for t in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert tools["run_python"]["approval"] == "allow" and tools["run_python"]["suggested_approval"] == "ask"
    bad = await hq.http.put(f"/api/tool-servers/{srv['id']}", json={**srv, "approval_rules": {"x": "maybe"}})
    assert bad.status_code == 422


async def test_call_waits_for_approval_then_runs(hq, workdir, monkeypatch):
    from app import subagents
    sent = []

    async def fake_notify(e):
        sent.append(e)
        return True

    monkeypatch.setattr(subagents, "notify", fake_notify)
    await hq.start_worker()
    _, _, t = await _setup(hq, workdir)
    await hq.say(t["id"], "what is 6*7?")
    [req] = await _pending(hq)
    assert req["tool"] == "run_python" and req["args"] == {"code": "print(6*7)"} and req["agent_name"] == "Flint"
    assert (await hq.http.get("/api/inbox/count")).json()["approvals"] == 1
    card = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "tool_approval"]
    assert card and card[0].meta["status"] == "pending"
    assert sent[0]["type"] == "tool_approval" and sent[0]["tool"] == "run_python"
    r = await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": True})
    assert r.json()["status"] == "approved"
    assert await hq.wait_agent_replies(t["id"], 1) == ["42"]
    card = [m for m in hq.messages(t["id"]) if m.meta.get("kind") == "tool_approval"][0]
    assert card.meta["status"] == "approved"
    again = await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": False})
    assert again.status_code == 409


async def test_denied_call_is_not_run(hq, workdir):
    await hq.start_worker()
    _, _, t = await _setup(hq, workdir)
    await hq.say(t["id"], "what is 6*7?")
    [req] = await _pending(hq)
    await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": False, "note": "not now"})
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "denied" in reply and "not now" in reply and "42" not in reply


async def test_remembered_approval_skips_the_next_ask(hq, workdir):
    await hq.start_worker()
    _, _, t = await _setup(hq, workdir, model="fake:coder-2")
    await hq.say(t["id"], "twice")
    [req] = await _pending(hq)
    await hq.http.post(f"/api/tool-approvals/{req['id']}/decide", json={"approve": True, "remember_minutes": 60})
    assert await hq.wait_agent_replies(t["id"], 1) == ["42 | 42"]
    with db.session() as s:
        assert len(s.exec(select(ToolApproval)).all()) == 1  # asked once, not twice


async def test_your_rules_override_the_server(hq, workdir):
    await hq.start_worker()
    _, _, t = await _setup(hq, workdir, rules={"run_python": "allow"})
    await hq.say(t["id"], "go")
    assert await hq.wait_agent_replies(t["id"], 1) == ["42"]
    _, _, t2 = await _setup_named(hq, workdir, "bench2", {"*": "block"})
    await hq.say(t2["id"], "go")
    [reply] = await hq.wait_agent_replies(t2["id"], 1)
    assert reply.startswith("Not allowed: the user has blocked run_python")


async def _setup_named(hq, workdir, name, rules):
    launch = ("import sys; sys.path.insert(0, %r); import workbench_mcp as w; "
              "w.ensure_venv(); w.mcp.run()") % str(WB_DIR)
    srv = (await hq.http.post("/api/tool-servers", json={
        "name": name, "transport": "stdio", "command": sys.executable, "args": ["-c", launch],
        "env": {"WORKBENCH_DIR": str(workdir)}, "approval_rules": rules})).json()
    a = await hq.agent(name=f"Agent-{name}", model="fake:coder", tool_server_ids=[srv["id"]])
    return srv, a, await hq.thread(a["id"])


async def test_no_decision_in_time(hq, workdir, monkeypatch):
    monkeypatch.setenv("AHQ_APPROVAL_TIMEOUT_MINUTES", "0.02")  # ~1 second
    await hq.start_worker()
    _, _, t = await _setup(hq, workdir)
    await hq.say(t["id"], "go")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert "didn't approve run_python" in reply
    [row] = (await hq.http.get("/api/tool-approvals")).json()
    assert row["status"] == "expired"
    late = await hq.http.post(f"/api/tool-approvals/{row['id']}/decide", json={"approve": True})
    assert late.status_code == 409
