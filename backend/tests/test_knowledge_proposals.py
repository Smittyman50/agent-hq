"""Agents suggesting documents for the knowledge library: Agent Findings by default,
other collections only where allowed, approve with edits (or move, or replace),
reject with a note, from a file, from a reflection, and the limits."""

from __future__ import annotations

from pydantic_ai import ModelResponse, TextPart, ToolCallPart

from app import knowledge, knowledge_proposals as kprop, learning
from app.runtime import load_agent

from .conftest import SCRIPTS, _last_prompt, _tool_returns
from .test_learning import FAST, _reflections, _until

RUNBOOK = ("## Steps\n1. Check the UPS load on the NUT dashboard.\n2. If above 80%, shed the lab "
           "rack first.\n3. Confirm the NAS shut down cleanly before power returns.")


def suggester(messages, info):
    """Suggests a runbook; the prompt may name a collection ('into X') or a file (att_…)."""
    import re

    prompt = _last_prompt(messages)
    tools = {t.name for t in info.function_tools}
    if "suggest_knowledge" not in tools:
        return ModelResponse(parts=[TextPart("no suggestions for me")])
    targets = [r for r in _tool_returns(messages) if r.tool_name == "knowledge_targets"]
    done = [r for r in _tool_returns(messages) if r.tool_name == "suggest_knowledge"]
    if not targets:
        return ModelResponse(parts=[ToolCallPart("knowledge_targets", {})])
    if not done:
        into = re.search(r"into (.+)$", prompt)
        f = re.search(r"att_[0-9a-f]{12}", prompt)
        args = {"title": "UPS failover runbook", "reason": "It worked today and will come up again.",
                "collection": into.group(1).strip() if into else ""}
        if f:
            args.update(attachment_id=f.group(0), title="Rack notes")
        else:
            args["content"] = RUNBOOK
        return ModelResponse(parts=[ToolCallPart("suggest_knowledge", args)])
    r = done[-1].content
    names = ",".join(t["collection"] for t in targets[-1].content)
    return ModelResponse(parts=[TextPart(f"{r['status']}: {r.get('collection') or r['summary']} | targets={names}")])


def kb_reflector(messages, info):
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "summary": "I looked back at this.", "skills": [], "lessons": [], "profile": [],
        "knowledge": [{"title": "NAS migration findings", "content": "## Findings\n" + RUNBOOK,
                       "reason": "A long investigation worth keeping."}]})])


SCRIPTS.update({"suggester": suggester, "kb-reflector": kb_reflector})


async def _ask(hq, agent_id, text, n=1):
    t = await hq.thread(agent_id)
    await hq.say(t["id"], text)
    return t, (await hq.wait_agent_replies(t["id"], n))[-1]


async def test_suggest_approve_with_edits(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:suggester")
    t, reply = await _ask(hq, a["id"], "write that up")
    assert reply == "suggested: Agent Findings | targets=Agent Findings"
    [p] = (await hq.http.get("/api/knowledge-proposals?status=pending")).json()
    assert p["collection"] == "Agent Findings" and p["agent_name"] == "Scarlett" and p["source"] == "tool"
    assert (await hq.http.get("/api/inbox/count")).json()["knowledge"] == 1
    card = next(m for m in hq.messages(t["id"]) if m.meta.get("kind") == "knowledge_proposal")
    assert card.meta["knowledge_proposal_id"] == p["id"] and card.meta["status"] == "pending"
    findings = next(c for c in (await hq.http.get("/api/knowledge/collections")).json() if c["name"] == "Agent Findings")
    assert findings["proposals"] == "all" and findings["docs"] == 0

    r = await hq.http.post(f"/api/knowledge-proposals/{p['id']}/decide",
                           json={"approve": True, "title": "UPS failover (lab)", "text": RUNBOOK + "\n4. Log it."})
    got = r.json()
    assert got["status"] == "approved" and got["doc_id"]
    doc = (await hq.http.get(f"/api/knowledge/docs/{got['doc_id']}")).json()
    assert doc["title"] == "UPS failover (lab)" and doc["link"] == f"/chat/{t['id']}"
    assert "Log it" in doc["text"] and doc["tags"] == ["agent/scarlett"]
    [src] = (await hq.http.get(f"/api/knowledge/collections/{findings['id']}/sources")).json()
    assert src["kind"] == "agent" and src["name"] == "Suggested by agents" and src["docs"] == 1
    hits = await knowledge.search("shed the lab rack", collection_ids=[findings["id"]])
    assert hits and hits[0].doc.id == got["doc_id"]
    assert next(m for m in hq.messages(t["id"]) if m.id == card.id).meta["status"] == "approved"
    assert (await hq.http.post(f"/api/knowledge-proposals/{p['id']}/decide", json={"approve": False})).status_code == 409
    # Agent-suggested documents can be deleted like uploads.
    assert (await hq.http.delete(f"/api/knowledge/docs/{got['doc_id']}")).status_code == 200


async def test_where_agents_may_suggest(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:suggester")
    b = await hq.agent(name="Duke", model="fake:echo")
    mine = (await hq.http.post("/api/knowledge/collections", json={"name": "Runbooks"})).json()
    assert mine["proposals"] == "off"
    _, reply = await _ask(hq, a["id"], "save it into Runbooks")
    assert reply.startswith("not suggested: you may not suggest documents for 'Runbooks'")

    await hq.http.put(f"/api/knowledge/collections/{mine['id']}",
                      json={"name": "Runbooks", "proposals": "chosen", "proposer_agent_ids": [b["id"]]})
    _, reply = await _ask(hq, a["id"], "save it into Runbooks")
    assert "may not suggest" in reply  # chosen, but not Scarlett
    await hq.http.put(f"/api/knowledge/collections/{mine['id']}",
                      json={"name": "Runbooks", "proposals": "chosen", "proposer_agent_ids": [a["id"]]})
    _, reply = await _ask(hq, a["id"], "save it into runbooks")
    assert reply == "suggested: Runbooks | targets=Agent Findings,Runbooks"
    # Only collections it can read.
    await hq.http.put(f"/api/knowledge/collections/{mine['id']}",
                      json={"name": "Runbooks", "proposals": "all", "agent_ids": [b["id"]]})
    assert [c.name for c in kprop.targets(load_agent(a["id"]))] == ["Agent Findings"]
    assert (await hq.http.put(f"/api/knowledge/collections/{mine['id']}",
                              json={"name": "Runbooks", "proposals": "maybe"})).status_code == 422


async def test_reject_move_replace_and_limits(hq, monkeypatch):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:suggester")
    other = (await hq.http.post("/api/knowledge/collections", json={"name": "Homelab"})).json()
    _, _ = await _ask(hq, a["id"], "write it up")
    [p] = (await hq.http.get("/api/knowledge-proposals?status=pending")).json()
    r = await hq.http.post(f"/api/knowledge-proposals/{p['id']}/decide",
                           json={"approve": False, "note": "We already have this in Obsidian."})
    assert r.json()["status"] == "rejected"
    # The same suggestion again: the agent is told it was turned down, and why.
    _, reply = await _ask(hq, a["id"], "write it up again")
    assert "suggested already (rejected); the user's note: We already have this in Obsidian." in reply

    # Approve into another collection (you may put it anywhere).
    me = load_agent(a["id"])
    got = await hq.runtime.suggest_knowledge(me, title="Rack power budget", text="## Budget\n" + RUNBOOK,
                                             reason="useful", thread_id=(await hq.thread(a["id"]))["id"])
    r = await hq.http.post(f"/api/knowledge-proposals/{got['proposal_id']}/decide",
                           json={"approve": True, "collection_id": other["id"]})
    first = r.json()
    assert first["collection"] == "Homelab"

    # A near-copy is flagged, and can replace the earlier agent document.
    got = await hq.runtime.suggest_knowledge(me, title="Rack power budget v2", text="## Budget\n" + RUNBOOK,
                                             reason="updated", collection="", thread_id=first["thread_id"])
    p2 = (await hq.http.get(f"/api/knowledge-proposals/{got['proposal_id']}")).json()
    assert p2["collection"] == "Agent Findings" and p2["similar_doc_id"] is None  # different collection
    r = await hq.http.post(f"/api/knowledge-proposals/{p2['id']}/decide",
                           json={"approve": True, "collection_id": other["id"], "replace_doc_id": first["doc_id"]})
    assert r.json()["doc_id"] == first["doc_id"]
    doc = (await hq.http.get(f"/api/knowledge/docs/{first['doc_id']}")).json()
    assert doc["title"] == "Rack power budget v2"

    # Too short, too long, too many waiting.
    assert (await hq.runtime.suggest_knowledge(me, title="x", text="short", reason="", thread_id=first["thread_id"]))["status"] == "not suggested"
    monkeypatch.setattr(kprop, "MAX_PENDING_PER_AGENT", 1)
    await hq.runtime.suggest_knowledge(me, title="One", text="## One\n" + RUNBOOK + " one", reason="", thread_id=first["thread_id"])
    out = await hq.runtime.suggest_knowledge(me, title="Two", text="## Two\n" + RUNBOOK + " two", reason="", thread_id=first["thread_id"])
    assert "waiting for the user" in out["summary"]


async def test_similar_document_is_flagged(hq):
    await hq.start_worker()
    me = load_agent((await hq.agent(name="Scarlett", model="fake:suggester"))["id"])
    t = await hq.thread(me.id)
    got = await hq.runtime.suggest_knowledge(me, title="UPS failover runbook", text=RUNBOOK, reason="", thread_id=t["id"])
    await hq.http.post(f"/api/knowledge-proposals/{got['proposal_id']}/decide", json={"approve": True})
    got = await hq.runtime.suggest_knowledge(me, title="UPS failover steps", text=RUNBOOK, reason="", thread_id=t["id"])
    assert "looks close to “UPS failover runbook”" in got["note"]
    p = (await hq.http.get(f"/api/knowledge-proposals/{got['proposal_id']}")).json()
    assert p["similar_title"] == "UPS failover runbook" and p["similar_replaceable"] is True


async def test_from_a_file_and_switches(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:suggester")
    t = await hq.thread(a["id"])
    r = await hq.http.post(f"/api/threads/{t['id']}/attachments",
                           files=[("files", ("rack.md", ("# Rack\n" + RUNBOOK).encode(), "text/markdown"))])
    f = r.json()[0]
    await hq.http.post(f"/api/threads/{t['id']}/messages", json={"content": "keep this", "attachment_ids": [f["id"]]})
    (reply,) = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("suggested")
    [p] = (await hq.http.get("/api/knowledge-proposals")).json()
    assert p["attachment_id"] == f["id"] and p["title"] == "Rack notes" and "shed the lab rack" in p["text"]

    # Learning off for the agent, or suggestions off for everyone: no tool.
    b = await hq.agent(name="Quiet", model="fake:suggester", learning_mode="off")
    assert (await _ask(hq, b["id"], "write it up"))[1] == "no suggestions for me"
    await hq.http.put("/api/knowledge/settings", json={"agent_suggestions": False})
    assert (await _ask(hq, a["id"], "write it up"))[1] == "no suggestions for me"


async def test_a_reflection_suggests_a_document(hq):
    await hq.http.put("/api/learning/settings", json={**FAST, "model": "fake:kb-reflector"})
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "dig into the NAS")
    await hq.wait_agent_replies(t["id"], 1)
    await hq.say(t["id"], "No, I meant the old NAS")
    await hq.wait_agent_replies(t["id"], 2)
    await _until(_reflections)
    [r] = await learning.run_due(hq.runtime)
    assert r.status == "done" and len(r.knowledge_proposal_ids) == 1
    [p] = (await hq.http.get("/api/knowledge-proposals")).json()
    assert p["source"] == "reflection" and p["reflection_id"] == r.id and p["title"] == "NAS migration findings"
