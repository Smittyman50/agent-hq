"""Tool servers: showing only some tools to agents, and marking a whole server read-only."""

from __future__ import annotations

import sys
from pathlib import Path

LAB = Path(__file__).with_name("lab_mcp_server.py")


async def _lab(hq, tmp_path, **extra):
    r = await hq.http.post("/api/tool-servers", json={
        "name": "lab", "transport": "stdio", "command": sys.executable, "args": [str(LAB)],
        "env": {"LAB_DIR": str(tmp_path)}, **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def test_hidden_tools_are_not_offered(hq, tmp_path):
    await hq.start_worker()
    srv = await _lab(hq, tmp_path, shown_tools=["list_vms"])
    probed = {t["name"]: t for t in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert probed["list_vms"]["shown"] is True and probed["restart_vm"]["shown"] is False
    a = await hq.agent(name="Ops", model="fake:operator", tool_server_ids=[srv["id"]])
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "restart web")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    # The model never saw restart_vm, so it couldn't call it.
    assert "['web', 'db']" in reply and "Restart: None" in reply
    assert not (tmp_path / "restarted-web").exists()


async def test_shown_list_is_kept_unless_sent(hq, tmp_path):
    srv = await _lab(hq, tmp_path, shown_tools=[" list_vms ", "list_vms"])
    assert srv["shown_tools"] == ["list_vms"]
    r = await hq.http.put(f"/api/tool-servers/{srv['id']}", json={**{k: v for k, v in srv.items() if k != "shown_tools"}})
    assert r.json()["shown_tools"] == ["list_vms"]
    r = await hq.http.put(f"/api/tool-servers/{srv['id']}", json={**srv, "shown_tools": []})
    assert r.json()["shown_tools"] == []


async def test_read_only_server_counts_every_tool_as_read_only(hq, tmp_path):
    srv = await _lab(hq, tmp_path, read_only=True)
    probed = {t["name"]: t for t in (await hq.http.get(f"/api/tool-servers/{srv['id']}/tools")).json()}
    assert probed["restart_vm"]["read_only_hint"] is True and probed["restart_vm"]["safe_in_tests"] is True
    # So webhook caution doesn't ask before it: only mark servers you know can't change things.
    await hq.start_worker()
    a = await hq.agent(name="Ops", model="fake:operator", tool_server_ids=[srv["id"]])
    hook = (await hq.http.post("/api/webhooks", json={"name": "issues"})).json()
    await hq.http.post("/api/subscriptions", json={"name": "i", "topic_filter": "webhook/issues/#",
                                                   "agent_id": a["id"], "min_interval_seconds": 0})
    await hq.anon.post("/hooks/issues", content=b"x", headers={"X-Webhook-Token": hook["secret"]})
    thread_id = (await hq.http.get("/api/subscriptions")).json()[0]["thread_id"]
    [reply] = await hq.wait_agent_replies(thread_id, 1)
    assert "web restarted" in reply
