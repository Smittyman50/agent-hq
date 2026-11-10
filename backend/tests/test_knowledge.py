"""The knowledge library: parsing, chunking, sources (uploads, web pages, Git / Obsidian),
search, recall, access and turning a runbook into skill proposals."""

from __future__ import annotations

import asyncio
import functools
import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from app import knowledge
from app.models import KnowledgeSource

from .test_learning import _until
from .test_memory import embed_server, use_embeddings  # noqa: F401  (fixture)

VAULT_NOTE = """---
title: Proxmox backups
tags: [homelab, backups]
aliases: [PBS]
---
# Proxmox backups

Backups run nightly at 02:00 to the PBS datastore on [[rewind]]. %%private: old schedule%%
See [[Runbooks/Restore|the restore runbook]] for restores. #proxmox

## Verify jobs
Check the verify job in PBS after each run.

```bash
# not a tag
proxmox-backup-client list
```
"""


def test_obsidian_note_parsing():
    n = knowledge.parse_note("Homelab/Backups.md", VAULT_NOTE, obsidian=True)
    assert n.title == "Proxmox backups"
    assert set(n.tags) == {"homelab", "backups", "proxmox"}  # not "not" from the code block
    assert n.links == ["Runbooks/Restore", "rewind"]
    assert "private" not in n.text and "the restore runbook" in n.text and "[[" not in n.text
    assert n.text.startswith("(Also known as: PBS)")
    plain = knowledge.parse_note("guide.md", "# Setup guide\nInstall it.", obsidian=False)
    assert plain.title == "Setup guide" and plain.tags == []


def test_chunking_follows_headings_and_splits_long_sections():
    text = "# A\nintro\n## B\n" + "\n\n".join(f"paragraph {i} " + "x" * 300 for i in range(12)) + "\n## C\nshort"
    parts = knowledge.chunk_markdown(text, max_chars=1000)
    heads = [h for h, _ in parts]
    assert heads[0] == "A" and "A › B" in heads and len([h for h in heads if h == "A › B"]) >= 3
    assert all(len(b) <= 1000 for _, b in parts)
    assert parts[-1][1].endswith("short")
    # Headings inside code blocks are not headings.
    assert len(knowledge.chunk_markdown("# T\n```\n# comment\n```\nend")) == 1


def test_include_and_exclude_globs():
    inc, exc = knowledge.DEFAULT_INCLUDE, knowledge.DEFAULT_EXCLUDE
    assert knowledge.matches("note.md", inc, exc) and knowledge.matches("a/b/c.md", inc, exc)
    assert not knowledge.matches(".obsidian/workspace.md", inc, exc)
    assert not knowledge.matches("img/photo.png", inc, exc)
    assert knowledge.matches("Journal/2026.md", inc, exc) and not knowledge.matches(
        "Journal/2026.md", inc, exc + ["Journal/**"])


async def _collection(hq, **kw):
    r = await hq.http.post("/api/knowledge/collections", json={"name": "Homelab", **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def _wait_ready(hq, source_id):
    async def ready():
        for _ in range(200):
            rows = (await hq.http.get(f"/api/knowledge/collections/{cid}/sources")).json()
            x = next(r for r in rows if r["id"] == source_id)
            if x["status"] in ("ready", "error"):
                return x
            await asyncio.sleep(0.05)
        raise AssertionError("still indexing")
    with knowledge.db.session() as s:
        cid = s.get(KnowledgeSource, source_id).collection_id
    return await ready()


async def test_uploads_search_recall_and_tools(hq, embed_server):  # noqa: F811
    await use_embeddings(hq, embed_server)
    await hq.start_worker()
    col = await _collection(hq)
    src = (await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources", json={"kind": "upload"})).json()
    assert src["status"] == "ready" and src["name"] == "Uploads"
    files = [("files", ("backups.md", VAULT_NOTE.encode(), "text/markdown")),
             ("files", ("ups.txt", b"The UPS is an APC SMT1500 in the rack. Battery replaced 2025.", "text/plain")),
             ("files", ("manual.html", b"<html><head><title>UPS manual</title></head><body><p>Runtime 25 min.</p></body></html>", "text/html")),
             ("files", ("photo.png", b"\x89PNG", "image/png"))]
    r = await hq.http.post(f"/api/knowledge/sources/{src['id']}/files", files=files)
    body = r.json()
    assert [a["path"] for a in body["added"]] == ["backups.md", "ups.txt", "manual.html"]
    assert "unsupported" in body["errors"][0] and body["added"][2]["title"] == "UPS manual"
    x = await _wait_ready(hq, src["id"])
    assert x["docs"] == 3 and x["chunks"] >= 2 and x["status"] == "ready"
    res = (await hq.http.get("/api/knowledge/search", params={"q": "when do backups run to the datastore"})).json()
    top = res["results"][0]
    assert top["title"] == "Proxmox backups" and top["sim"] is not None and "02:00" in top["text"]
    docs = (await hq.http.get(f"/api/knowledge/collections/{col['id']}/docs")).json()
    assert docs["total"] == 3 and all(d["indexed"] for d in docs["docs"])
    d = (await hq.http.get(f"/api/knowledge/docs/{top['doc_id']}")).json()
    assert d["sections"] and all(s["embedded"] for s in d["sections"])

    # An agent gets excerpts and the tools; the reply cites what it found.
    a = await hq.agent(name="Duke", model="fake:reader")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "when do the backups run to the datastore")
    [reply] = await hq.wait_agent_replies(t["id"], 1)
    assert reply.startswith("found Proxmox backups / Proxmox backups") and reply.endswith("excerpts=True")

    # Turning recall off still leaves the tools.
    await hq.http.put(f"/api/knowledge/collections/{col['id']}", json={**col, "auto_recall": False})
    await hq.say(t["id"], "when do the backups run to the datastore")
    assert (await hq.wait_agent_replies(t["id"], 2))[-1].endswith("excerpts=False")

    # Deleting an upload removes it from search.
    await hq.http.delete(f"/api/knowledge/docs/{top['doc_id']}")
    res = (await hq.http.get("/api/knowledge/search", params={"q": "backups datastore"})).json()
    assert all(r["title"] != "Proxmox backups" for r in res["results"])


async def test_collections_are_private_to_the_agents_you_pick(hq):
    await hq.start_worker()
    a = await hq.agent(name="Scarlett", model="fake:reader")
    b = await hq.agent(name="Flint", model="fake:reader")
    col = await _collection(hq, name="Novel notes", agent_ids=[a["id"]])
    src = (await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources", json={"kind": "upload"})).json()
    await hq.http.post(f"/api/knowledge/sources/{src['id']}/files",
                       files=[("files", ("plot.md", b"# Plot\n## Act one\nThe lattice is found in Shinar.", "text/markdown"))])
    await _wait_ready(hq, src["id"])
    ta, tb = await hq.thread(a["id"]), await hq.thread(b["id"])
    await hq.say(ta["id"], "where is the lattice found")
    await hq.say(tb["id"], "where is the lattice found")
    [ra] = await hq.wait_agent_replies(ta["id"], 1)
    [rb] = await hq.wait_agent_replies(tb["id"], 1)
    assert ra.startswith("found Plot / Plot › Act one") and rb == "no library; excerpts=False"
    seen = (await hq.http.get("/api/knowledge/search", params={"q": "lattice Shinar", "agent_id": b["id"]})).json()
    assert seen["results"] == []


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin"})


async def test_obsidian_vault_from_git_syncs_changes(hq, tmp_path):
    origin, work = tmp_path / "vault.git", tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "Homelab").mkdir()
    (work / ".obsidian").mkdir()
    (work / "Homelab" / "Backups.md").write_text(VAULT_NOTE)
    (work / "Homelab" / "UPS.md").write_text("# UPS\nAPC SMT1500, battery replaced 2025. #power")
    (work / ".obsidian" / "app.md").write_text("settings")
    (work / "image.png").write_bytes(b"\x89PNG")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "vault")
    _git(work, "push", "origin", "HEAD:main")

    col = await _collection(hq)
    with knowledge.db.session() as s:  # a local path stands in for https://gitea/... here
        src = KnowledgeSource(collection_id=col["id"], kind="git", name="Vault", url=str(origin),
                              obsidian=True, vault_name="Mike", include=list(knowledge.DEFAULT_INCLUDE),
                              exclude=list(knowledge.DEFAULT_EXCLUDE), status="queued")
        s.add(src)
        s.commit()
        s.refresh(src)
    [done] = await knowledge.run_due()
    assert done.status == "ready" and done.docs == 2 and done.branch == "main" and done.last_commit
    docs = {d["path"]: d for d in (await hq.http.get(f"/api/knowledge/collections/{col['id']}/docs")).json()["docs"]}
    assert set(docs) == {"Homelab/Backups.md", "Homelab/UPS.md"}
    b = docs["Homelab/Backups.md"]
    assert b["link"] == "obsidian://open?vault=Mike&file=Homelab/Backups" and "proxmox" in b["tags"]
    first_hash = b["hash"]

    # Change one note, delete the other, add a new one: only those are touched.
    (work / "Homelab" / "Backups.md").write_text(VAULT_NOTE.replace("02:00", "03:30"))
    (work / "Homelab" / "UPS.md").unlink()
    (work / "Ideas.md").write_text("# Ideas\nA SearXNG instance for agent web search.")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "edit")
    _git(work, "push", "origin", "HEAD:main")
    assert knowledge.claim(src.id)
    again = await knowledge.index_source(src.id)
    assert again.docs == 2 and again.last_commit != done.last_commit
    docs = {d["path"]: d for d in (await hq.http.get(f"/api/knowledge/collections/{col['id']}/docs")).json()["docs"]}
    assert set(docs) == {"Homelab/Backups.md", "Ideas.md"} and docs["Homelab/Backups.md"]["hash"] != first_hash
    res = (await hq.http.get("/api/knowledge/search", params={"q": "backups 03:30"})).json()
    assert "03:30" in res["results"][0]["text"]
    # Synced documents can't be deleted here (they'd come back); the source can.
    assert (await hq.http.delete(f"/api/knowledge/docs/{docs['Ideas.md']['id']}")).status_code == 422
    await hq.http.delete(f"/api/knowledge/sources/{src.id}")
    assert (await hq.http.get(f"/api/knowledge/collections/{col['id']}/docs")).json()["total"] == 0


async def test_git_errors_are_reported_without_the_token(hq):
    col = await _collection(hq)
    r = await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources", json={
        "kind": "git", "url": "http://127.0.0.1:9/mike/vault.git", "username": "agent-hq", "token": "s3cret-token",
        "obsidian": True})
    x = r.json()
    assert "token" not in x and x["has_token"] and x["name"] == "vault" and ".obsidian/**" in x["exclude"]
    x = await _wait_ready(hq, x["id"])
    assert x["status"] == "error" and "s3cret-token" not in x["error"]
    # Saving without a token keeps the saved one.
    r = await hq.http.put(f"/api/knowledge/sources/{x['id']}", json={**x, "token": None})
    assert r.json()["has_token"]
    assert (await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources",
                               json={"kind": "git", "url": "ftp://x"})).status_code == 422


@pytest.fixture
def web_server(tmp_path):
    (tmp_path / "page.html").write_text("<html><head><title>UPS manual</title></head><body>"
                                        "<h1>Runtime</h1><p>At 40% load the SMT1500 runs 25 minutes.</p></body></html>")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def test_web_page_source(hq, web_server):
    col = await _collection(hq)
    x = (await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources",
                            json={"kind": "url", "url": f"{web_server}/page.html"})).json()
    x = await _wait_ready(hq, x["id"])
    assert x["status"] == "ready" and x["docs"] == 1
    [d] = (await hq.http.get(f"/api/knowledge/collections/{col['id']}/docs")).json()["docs"]
    assert d["title"] == "UPS manual" and d["link"].endswith("/page.html")
    full = (await hq.http.get(f"/api/knowledge/docs/{d['id']}")).json()
    assert "25 minutes" in full["text"]


async def test_a_runbook_becomes_skill_proposals(hq):
    await hq.http.put("/api/learning/settings", json={"model": "fake:docreader"})
    a = await hq.agent(name="Duke", model="fake:echo")
    col = await _collection(hq)
    src = (await hq.http.post(f"/api/knowledge/collections/{col['id']}/sources", json={"kind": "upload"})).json()
    up = (await hq.http.post(f"/api/knowledge/sources/{src['id']}/files", files=[(
        "files", ("runbook.md", b"# Runbook\n## Restore a VM\nUse PBS.\n## Rotate the UPS battery\nEvery 3 years.",
                  "text/markdown"))])).json()
    doc_id = up["added"][0]["id"]
    r = await hq.http.post(f"/api/knowledge/docs/{doc_id}/propose-skills", json={"agent_id": a["id"]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [p["name"] for p in out["proposals"]] == ["restore_a_vm", "rotate_the_ups_battery"]
    pending = (await hq.http.get("/api/skill-proposals?status=pending")).json()
    assert {p["source"] for p in pending} == {"document"} and len(pending) == 2
    await hq.http.put(f"/api/agents/{a['id']}", json={**a, "learning_mode": "off"})
    r = await hq.http.post(f"/api/knowledge/docs/{doc_id}/propose-skills", json={"agent_id": a["id"]})
    assert r.status_code == 422 and "Off" in r.json()["detail"]


async def test_settings_validation(hq):
    r = await hq.http.put("/api/knowledge/settings", json={"recall_count": 99, "min_score": 2})
    assert r.json()["recall_count"] == 10 and r.json()["min_score"] == 1.0
    _ = Path  # keep import for type hints
