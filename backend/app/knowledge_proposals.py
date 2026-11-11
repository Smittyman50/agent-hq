"""Agents suggesting documents for the knowledge library; you approve each one.

An agent that wrote or found something worth keeping (a runbook it worked out, research
findings, a summary of a long thread) proposes it with `suggest_knowledge`, or its
reflection does after a job. The proposal waits in Requests → Knowledge and as a card in
the conversation. You can edit the title and text, move it to another collection, and
approve or reject it (with a note the agent is told if it suggests the same thing again).

Where agents may propose:
* **Agent Findings**, a collection created for this, open to every agent. It's where
  proposals go unless the agent names another collection.
* Any other collection whose "Agents may suggest documents" setting allows that agent
  ("Every agent" or chosen agents), and which the agent can read. Off by default, so
  your own collections only take suggestions when you say so.

Approved documents go into the collection's "Suggested by agents" source. They can be
deleted like uploads and are never written anywhere outside Agent HQ (not to your
Obsidian vault or Git sources).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlmodel import func, select

from . import db, knowledge
from .models import (AgentDef, KnowledgeCollection, KnowledgeDoc, KnowledgeProposal,
                     KnowledgeSource)

log = logging.getLogger("agent_hq.knowledge_proposals")

FINDINGS_NAME = "Agent Findings"
FINDINGS_DESCRIPTION = ("Documents agents suggested and you approved: runbooks they worked out, "
                        "research findings, summaries worth keeping.")
AGENT_SOURCE_NAME = "Suggested by agents"
MAX_CHARS = 60_000
MAX_PENDING_PER_AGENT = 5
# How alike the closest existing document must be to be shown as a possible duplicate.
SIMILAR_SIM = 0.88
SIMILAR_KEYWORD = 0.9

SUGGEST_RULES = """\
Knowledge library suggestions: when your work produced something worth keeping for
the whole team (a runbook or procedure that worked, research findings, a reference
summary), suggest it with `suggest_knowledge`. The user approves each one. Not for
one-off answers, today's numbers, or things already in the library. Suggestions go to
"Agent Findings" unless the user asked for another collection; `knowledge_targets` lists
the ones you may use."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def enabled() -> bool:
    return bool(knowledge.get_settings()["agent_suggestions"])


def findings_collection() -> KnowledgeCollection:
    """The Agent Findings collection, created the first time it's needed."""
    with db.session() as s:
        c = s.exec(select(KnowledgeCollection).where(KnowledgeCollection.name == FINDINGS_NAME)).first()
        if c is None:
            c = KnowledgeCollection(name=FINDINGS_NAME, description=FINDINGS_DESCRIPTION, proposals="all")
            s.add(c)
            s.commit()
            s.refresh(c)
        return c


def may_propose(agent: AgentDef, c: KnowledgeCollection) -> bool:
    if not c.enabled or c.proposals == "off":
        return False
    who = {agent.id} | ({agent.parent_agent_id} if agent.is_subagent and agent.parent_agent_id else set())
    if c.proposals == "chosen" and not who & set(c.proposer_agent_ids or []):
        return False
    return c.id in {x.id for x in knowledge.readable_collections(agent)}


def targets(agent: AgentDef) -> list[KnowledgeCollection]:
    """Collections this agent may suggest documents for (Agent Findings first)."""
    if not enabled():
        return []
    findings = findings_collection()
    with db.session() as s:
        rows = list(s.exec(select(KnowledgeCollection).order_by(KnowledgeCollection.name)).all())
    out = [c for c in rows if may_propose(agent, c)]
    out.sort(key=lambda c: c.id != findings.id)
    return out


def resolve_target(agent: AgentDef, name: str) -> KnowledgeCollection:
    """The collection an agent named (by name or ID), or Agent Findings. Raises ValueError."""
    allowed = targets(agent)
    if not allowed:
        raise ValueError("knowledge suggestions are turned off")
    want = (name or "").strip()
    if not want:
        findings = findings_collection()
        for c in allowed:
            if c.id == findings.id:
                return c
        raise ValueError("you may not suggest documents for Agent Findings; name a collection "
                         "from knowledge_targets")
    for c in allowed:
        if c.id == want or c.name.lower() == want.lower():
            return c
    raise ValueError(f"you may not suggest documents for '{want}'. You may use: "
                     + ", ".join(c.name for c in allowed))


def pending_count(agent_id: str | None = None) -> int:
    with db.session() as s:
        q = select(func.count()).select_from(KnowledgeProposal).where(KnowledgeProposal.status == "pending")
        if agent_id:
            q = q.where(KnowledgeProposal.agent_id == agent_id)
        return s.exec(q).one()


def _norm(t: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", t.lower()).split())


def already_suggested(collection_id: str, title: str, text: str) -> KnowledgeProposal | None:
    """The same suggestion waiting or turned down already (same title, or same text)."""
    nt, nx = _norm(title), _norm(text)[:2000]
    with db.session() as s:
        rows = s.exec(select(KnowledgeProposal).where(
            KnowledgeProposal.collection_id == collection_id,
            KnowledgeProposal.status.in_(("pending", "rejected")))).all()
    for p in rows:
        if (nt and _norm(p.title) == nt) or (nx and _norm(p.text)[:2000] == nx):
            return p
    return None


async def closest(collection_id: str, title: str, text: str) -> tuple[KnowledgeDoc | None, float | None]:
    """The most similar document already in the collection, if it looks like a duplicate."""
    try:
        hits = await knowledge.search(f"{title}\n{text[:1500]}", collection_ids=[collection_id], k=1)
    except Exception as e:  # a search problem must not stop the suggestion
        log.info("similarity check failed: %s", e)
        return None, None
    if not hits:
        return None, None
    h = hits[0]
    if (h.sim is not None and h.sim >= SIMILAR_SIM) or (h.sim is None and h.keyword >= SIMILAR_KEYWORD):
        return h.doc, round(h.sim if h.sim is not None else h.keyword, 3)
    return None, None


def clean(title: str, text: str) -> tuple[str, str]:
    title = " ".join((title or "").split())[:200]
    text = (text or "").strip()
    if not title:
        m = re.search(r"^#\s+(.+)$", text, re.M)
        title = m.group(1).strip()[:200] if m else ""
    if not title:
        raise ValueError("a suggestion needs a title")
    if len(text) < 40:
        raise ValueError("there is too little text to keep (at least a few sentences)")
    if len(text) > MAX_CHARS:
        raise ValueError(f"that's too long for one document ({len(text):,} characters; at most "
                         f"{MAX_CHARS:,}). Split it, or summarise it.")
    return title, text


# --------------------------------------------------------------------------- deciding


def agent_source(collection_id: str) -> KnowledgeSource:
    """The collection's "Suggested by agents" source, created on first use."""
    with db.session() as s:
        src = s.exec(select(KnowledgeSource).where(KnowledgeSource.collection_id == collection_id,
                                                   KnowledgeSource.kind == "agent")).first()
        if src is None:
            src = KnowledgeSource(collection_id=collection_id, kind="agent", name=AGENT_SOURCE_NAME,
                                  status="ready")
            s.add(src)
            s.commit()
            s.refresh(src)
        return src


def _slug(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60] or "document"


async def approve(p: KnowledgeProposal, *, replace_doc_id: str | None = None) -> KnowledgeDoc:
    """Write the document into the collection and index it."""
    src = agent_source(p.collection_id)
    path = f"agents/{p.id}-{_slug(p.title)}.md"
    if replace_doc_id:
        with db.session() as s:
            old = s.get(KnowledgeDoc, replace_doc_id)
        if old is None or old.collection_id != p.collection_id:
            raise ValueError("that document isn't in this collection")
        if old.source_id != src.id:
            raise ValueError("only documents agents suggested can be replaced this way; "
                             "update your own files at their source")
        path = old.path
    text = p.text if p.text.lstrip().startswith("#") else f"# {p.title}\n\n{p.text}"
    doc, _ = knowledge.upsert_doc(src, path, title=p.title, text=text,
                                  link=f"/chat/{p.thread_id}" if p.thread_id else "",
                                  tags=[f"agent/{_slug(p.agent_name)}"] if p.agent_name else [])
    await knowledge.index_doc(doc.id)
    docs, chunks = knowledge._counts(src.id)
    knowledge._set(src.id, docs=docs, chunks=chunks, last_indexed_at=_now())
    return doc


async def decide(proposal_id: str, approve_it: bool, *, note: str = "", title: str | None = None,
                 text: str | None = None, collection_id: str | None = None,
                 replace_doc_id: str | None = None) -> KnowledgeProposal:
    """Approve (optionally with your edits, into another collection) or reject."""
    with db.session() as s:
        p = s.get(KnowledgeProposal, proposal_id)
    if p is None:
        raise LookupError("no such suggestion")
    if p.status != "pending":
        raise ValueError(f"this suggestion is already {p.status}")
    if title is not None or text is not None:
        p.title, p.text = clean(title if title is not None else p.title,
                                text if text is not None else p.text)
    if collection_id and collection_id != p.collection_id:
        with db.session() as s:
            if s.get(KnowledgeCollection, collection_id) is None:
                raise LookupError("no such collection")
        p.collection_id = collection_id  # you may put it anywhere, whatever the agent was allowed
        p.similar_doc_id, p.similar_title, p.similar_score = None, "", None
    if approve_it:
        doc = await approve(p, replace_doc_id=replace_doc_id)
        p.doc_id = doc.id
    p.status = "approved" if approve_it else "rejected"
    p.note, p.decided_at = (note or "").strip()[:1000], _now()
    with db.session() as s:
        s.add(p)
        s.commit()
        s.refresh(p)
    return p


def out(p: KnowledgeProposal) -> dict[str, Any]:
    with db.session() as s:
        c = s.get(KnowledgeCollection, p.collection_id)
        similar = s.get(KnowledgeDoc, p.similar_doc_id) if p.similar_doc_id else None
        src = s.get(KnowledgeSource, similar.source_id) if similar else None
        replaceable = bool(src) and src.kind == "agent"
    return {**p.model_dump(), "collection": c.name if c else "(deleted collection)",
            "similar_replaceable": replaceable}
