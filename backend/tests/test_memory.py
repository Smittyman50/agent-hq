"""Long-term memory: search, scopes, lifecycle, recall, capture, tools, tidy-up, re-embed.
Core search/lifecycle tests also run on Postgres + pgvector (via PGlite) when available."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shutil
import socket
import subprocess
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Request
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo

from app import db, memory
from app.config import reload_settings

from .conftest import SCRIPTS, _instructions, _last_prompt, _tool_returns

EMBED_INPUTS: list[str] = []


def fake_vector(text: str, dims: int = 256) -> list[float]:
    v = [0.0] * dims
    for w in re.findall(r"[a-z0-9]+", text.lower().split(": ", 1)[-1]):
        h = int(hashlib.md5(w.encode()).hexdigest(), 16)
        v[h % dims] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/tags")
    def tags():
        return {"models": [{"name": "nomic-embed-text", "details": {}}]}

    @app.post("/api/embed")
    async def embed(req: Request):
        body = await req.json()
        EMBED_INPUTS.extend(body["input"])
        return {"embeddings": [fake_vector(t) for t in body["input"]]}

    return app


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def embed_server():
    port = _port()
    server = uvicorn.Server(uvicorn.Config(embed_app(), host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


async def use_embeddings(hq, url, model="nomic-embed-text"):
    p = (await hq.http.post("/api/providers", json={"name": "Ollama", "kind": "ollama", "base_url": url})).json()
    r = await hq.http.put("/api/memory/settings", json={"embedding_provider_id": p["id"], "embedding_model": model})
    assert r.status_code == 200
    return p


async def remember(hq, text, **kw):
    r = await hq.http.post("/api/memory", json={"text": text, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def search(hq, q, **params):
    return (await hq.http.get("/api/memory", params={"query": q, **params})).json()


SEED = ["pve1 root disk reached 95% because old PBS snapshots piled up; pruning them fixed it",
        "OPNsense WAN gateway alerts are usually the ISP; ignore unless they last over 10 minutes",
        "Mike prefers reports as short bullet lists with the action needed first"]


# --------------------------------------------------------------------------- search & lifecycle


async def _search_and_lifecycle(hq):
    for t in SEED:
        await remember(hq, t)
    hits = await search(hq, "pve1 disk full")
    assert hits[0]["text"].startswith("pve1 root disk")
    hits = await search(hq, "how should I format a report for Mike?")
    assert hits[0]["text"].startswith("Mike prefers")

    # Retire hides it; include_retired shows it with the reason.
    mid = (await search(hq, "WAN gateway"))[0]["id"]
    await hq.http.post(f"/api/memory/{mid}/retire", json={"reason": "switched ISP"})
    assert all(h["id"] != mid for h in await search(hq, "WAN gateway"))
    old = [h for h in await search(hq, "WAN gateway", include_retired=True) if h["id"] == mid][0]
    assert old["invalidated_reason"] == "switched ISP"
    await hq.http.post(f"/api/memory/{mid}/restore")
    assert (await search(hq, "WAN gateway"))[0]["id"] == mid

    # Supersede: new memory replaces old; old points to new.
    pve = (await search(hq, "pve1 disk"))[0]["id"]
    new = await remember(hq, "pve1 root disk was expanded to 512 GB on 2026-11-06", supersedes=pve)
    assert (await search(hq, "pve1 disk"))[0]["id"] == new["id"]
    retired = [h for h in await search(hq, "pve1 disk", include_retired=True) if h["id"] == pve][0]
    assert retired["superseded_by"] == new["id"]


async def test_keyword_search_without_embeddings(hq):
    await _search_and_lifecycle(hq)


async def test_vector_search_with_embeddings_and_prefixes(hq, embed_server):
    EMBED_INPUTS.clear()
    await use_embeddings(hq, embed_server)
    await _search_and_lifecycle(hq)
    assert any(i.startswith("search_document: ") for i in EMBED_INPUTS)
    assert any(i.startswith("search_query: ") for i in EMBED_INPUTS)
    assert all(m["embedded"] for m in (await hq.http.get("/api/memory")).json())


async def test_scopes(hq, embed_server):
    await use_embeddings(hq, embed_server)
    a = await hq.agent(name="A", model="fake:worker")
    b = await hq.agent(name="B", model="fake:worker")
    await remember(hq, "A's private note: the lab VPN key lives in vault path kv/vpn", scope="agent", agent_id=a["id"])
    await remember(hq, "Team note: the lab VPN uses WireGuard on port 51820")
    for_a = await memory.search("lab VPN", agent_id=a["id"])
    for_b = await memory.search("lab VPN", agent_id=b["id"])
    assert len(for_a) == 2 and len(for_b) == 1 and for_b[0].memory.text.startswith("Team note")


# --------------------------------------------------------------------------- runtime


def mem_echo(messages, info: AgentInfo) -> ModelResponse:
    ins = _instructions(messages)
    recalled = re.findall(r"^- \[\w+ · [\d-]+ · \w+ · \w+\] (.+)$", ins, re.M)
    has_tools = {"remember", "recall", "forget", "confirm"} <= {t.name for t in info.function_tools}
    return ModelResponse(parts=[TextPart(json.dumps({"recalled": recalled, "tools": has_tools}))])


def rememberer(messages, info: AgentInfo) -> ModelResponse:
    returns = _tool_returns(messages)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("remember", {
            "text": "rewind's PBS datastore is named TrueNAS", "kind": "fact"})])
    if returns[0].tool_name == "remember":
        mid = returns[0].content["saved"]
        return ModelResponse(parts=[ToolCallPart("forget", {"memory_id": mid, "reason": "testing forget"})])
    return ModelResponse(parts=[TextPart("done")])


def tidier(messages, info: AgentInfo) -> ModelResponse:
    ids = re.findall(r"id=(\w+)", _last_prompt(messages))
    return ModelResponse(parts=[TextPart(json.dumps({"actions": [
        {"type": "merge", "ids": ids, "text": "pve1 backups: nightly at 02:00 to PBS on rewind (merged)"}]}))])


SCRIPTS.update({"mem-echo": mem_echo, "rememberer": rememberer, "tidier": tidier})


async def test_recall_is_injected_into_instructions(hq, embed_server):
    await hq.start_worker()
    await use_embeddings(hq, embed_server)
    for t in SEED:
        await remember(hq, t)
    a = await hq.agent(name="Ops", model="fake:mem-echo")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "Is pve1 disk filling up again?")
    reply = json.loads((await hq.wait_agent_replies(t["id"], 1))[0])
    assert reply["tools"] and reply["recalled"][0].startswith("pve1 root disk")

    # Per-agent switch turns it all off.
    a["memory_enabled"] = False
    await hq.http.put(f"/api/agents/{a['id']}", json=a)
    await hq.say(t["id"], "Is pve1 disk filling up again?")
    reply = json.loads((await hq.wait_agent_replies(t["id"], 2))[1])
    assert reply == {"recalled": [], "tools": False}


async def test_delegation_results_are_captured_once(hq):
    await hq.start_worker()
    w = await hq.agent(name="Scout", description="research", model="fake:worker")
    b = await hq.agent(name="Chief", model="fake:boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    for n in (1, 2):
        await hq.say(t["id"], "check the RSS feeds")
        await hq.wait_agent_replies(t["id"], n)
    mems = (await hq.http.get("/api/memory")).json()
    captured = [m for m in mems if m["source"] == "auto:delegation"]
    assert len(captured) == 1 and captured[0]["times_seen"] == 2  # second time confirmed, not duplicated
    assert captured[0]["text"].startswith("Scout was asked: Please handle: check the RSS feeds")


async def test_failed_delegations_are_not_captured(hq):
    # A failure ("I can't browse" with a tool server missing) recalled next time would teach
    # the agent to give up again, so only completed work becomes memory.
    await hq.start_worker()
    w = await hq.agent(name="Snake Eyes", description="browses", model="fake:broken")
    b = await hq.agent(name="Duke", model="fake:boss", delegates=[w["id"]])
    t = await hq.thread(b["id"])
    await hq.say(t["id"], "open CNN")
    await hq.wait_agent_replies(t["id"], 1)
    assert not [m for m in (await hq.http.get("/api/memory")).json() if m["source"] == "auto:delegation"]


async def test_mqtt_events_are_captured(hq):
    await hq.start_worker()
    a = await hq.agent(name="Watcher", model="fake:alert")
    await hq.http.post("/api/subscriptions", json={"name": "alerts", "topic_filter": "homelab/#",
                                                   "agent_id": a["id"], "min_interval_seconds": 0})
    await hq.http.post("/api/subscriptions/test", json={"topic": "homelab/ups", "payload": "on battery"})
    thread = (await hq.http.get("/api/subscriptions")).json()[0]["thread_id"]
    await hq.wait_agent_replies(thread, 1)
    [m] = [m for m in (await hq.http.get("/api/memory")).json() if m["source"] == "auto:event"]
    assert "homelab/ups" in m["text"] and "on battery" in m["text"] and "Handled event" in m["text"]


async def test_agent_memory_tools(hq):
    await hq.start_worker()
    a = await hq.agent(name="Keeper", model="fake:rememberer")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "note the datastore name")
    await hq.wait_agent_replies(t["id"], 1)
    [m] = (await hq.http.get("/api/memory", params={"include_retired": True})).json()
    assert m["source"] == "agent" and m["created_by"] == a["id"]
    assert m["invalidated_reason"] == "testing forget"


# --------------------------------------------------------------------------- maintenance


async def _tidy_without_model(hq):
    await remember(hq, "pve1 backups run nightly at 02:00 to PBS on rewind")
    newer = await remember(hq, "pve1 backups run nightly at 02:00 to the PBS on rewind")
    report = (await hq.http.post("/api/memory/tidy")).json()
    assert report["retired"] == 1 and not report["model_used"]
    live = (await hq.http.get("/api/memory")).json()
    assert [m["id"] for m in live] == [newer["id"]]


async def test_tidy_dedupes_without_a_model(hq, embed_server):
    await use_embeddings(hq, embed_server)
    await _tidy_without_model(hq)


async def test_tidy_groups_team_memories_from_different_conversations(hq, embed_server):
    """Captured team memories record the conversation they came from; that mustn't stop
    the tidy-up from comparing them. Conversation-scoped ones stay apart."""
    from app import memory

    await use_embeddings(hq, embed_server)
    a = await hq.agent(name="Duke", model="fake:worker")
    t1, t2 = await hq.thread(a["id"]), await hq.thread(a["id"])
    text = "Snake Eyes was asked about Optimus Prime's gun on Wikipedia"
    await memory.add(text, kind="incident", scope="team", thread_id=t1["id"])
    newer = await memory.add(text + " page", kind="incident", scope="team", thread_id=t2["id"])
    await memory.add("thread note about pve1 backups", scope="thread", thread_id=t1["id"])
    await memory.add("thread note about pve1 backups.", scope="thread", thread_id=t2["id"])
    report = (await hq.http.post("/api/memory/tidy")).json()
    assert report["groups"] == 1 and report["retired"] == 1
    live = {m["id"] for m in (await hq.http.get("/api/memory")).json()}
    assert newer.id in live and len(live) == 3


async def test_tidy_merges_with_a_model(hq, embed_server):
    await use_embeddings(hq, embed_server)
    # The fake bag-of-words embeddings are crude, so loosen "related" for this test.
    await hq.http.put("/api/memory/settings", json={"tidy_model": "fake:tidier", "related_threshold": 0.3})
    await remember(hq, "pve1 backups run nightly at 02:00")
    await remember(hq, "pve1 backups go to the PBS server rewind")
    report = (await hq.http.post("/api/memory/tidy")).json()
    assert report["merged"] == 1 and report["model_used"]
    [m] = (await hq.http.get("/api/memory")).json()
    assert m["text"].endswith("(merged)") and m["source"] == "tidy"


async def test_reembed_after_choosing_a_model(hq, embed_server):
    for t in SEED:
        await remember(hq, t)
    assert not any(m["embedded"] for m in (await hq.http.get("/api/memory")).json())
    p = (await hq.http.post("/api/providers", json={"name": "O", "kind": "ollama", "base_url": embed_server})).json()
    await memory.reembed()  # nothing configured yet
    memory.save_settings({"embedding_provider_id": p["id"], "embedding_model": "nomic-embed-text"})
    assert (await memory.reembed())["embedded"] == 3
    stats = (await hq.http.get("/api/memory/stats")).json()
    assert stats["embedded_with_current_model"] == 3


# --------------------------------------------------------------------------- Postgres + pgvector


@pytest.fixture(scope="module")
def pglite_url(tmp_path_factory):
    """Postgres + pgvector via PGlite (WebAssembly Postgres) speaking the wire protocol."""
    npm, node = shutil.which("npm"), shutil.which("node")
    if not npm or not node:
        pytest.skip("node/npm not installed")
    d = tmp_path_factory.mktemp("pglite")
    r = subprocess.run([npm, "install", "--prefix", str(d), "--no-audit", "--no-fund",
                        "@electric-sql/pglite", "@electric-sql/pglite-socket", "@electric-sql/pglite-pgvector"],
                       capture_output=True, timeout=300)
    server = d / "node_modules/@electric-sql/pglite-socket/dist/scripts/server.js"
    if r.returncode != 0 or not server.exists():
        pytest.skip("could not install PGlite (offline?)")
    port = _port()
    proc = subprocess.Popen([node, str(server), "-p", str(port), "-e", "@electric-sql/pglite-pgvector:vector",
                             "-m", "12"], cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import psycopg
    for _ in range(120):
        try:
            psycopg.connect(f"postgresql://postgres@127.0.0.1:{port}/postgres").close()
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.terminate()
        pytest.skip("PGlite with pgvector did not start")
    yield f"postgresql://postgres@127.0.0.1:{port}/postgres"
    proc.terminate()


@pytest.fixture
async def pg_mem(hq, pglite_url, monkeypatch):
    monkeypatch.setenv("AHQ_DB_URL", pglite_url)
    reload_settings()
    db.reset_engine()
    from sqlalchemy import text
    from sqlmodel import SQLModel
    with db.engine().begin() as c:
        c.execute(text("DROP TABLE IF EXISTS memory CASCADE"))
    SQLModel.metadata.drop_all(db.engine())
    db.init_db()
    assert memory.pgvector_enabled()
    hq.sign_in()
    yield hq


async def test_postgres_pgvector_search_and_lifecycle(pg_mem, embed_server):
    await use_embeddings(pg_mem, embed_server)
    await _search_and_lifecycle(pg_mem)
    assert (await pg_mem.http.get("/api/memory/stats")).json()["pgvector"] is True


async def test_postgres_keyword_only(pg_mem):
    await _search_and_lifecycle(pg_mem)


async def test_postgres_tidy(pg_mem, embed_server):
    await use_embeddings(pg_mem, embed_server)
    await _tidy_without_model(pg_mem)
