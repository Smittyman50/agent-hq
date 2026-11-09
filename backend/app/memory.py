"""Long-term memory: Postgres (pgvector + full-text) or SQLite, with Ollama/OpenAI-style
embeddings from your model providers.

* search() blends vector similarity, keyword overlap (identifiers like `pve1` matter) and
  recency. Without an embedding model it still works on keywords alone.
* Memories are never silently overwritten. Outdated ones are retired (invalidated_at,
  reason, superseded_by), so history stays queryable.
* capture() skips near-duplicates: it confirms the existing memory instead.
* tidy() merges duplicates and, with a model configured, resolves contradictions.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text as sql
from sqlmodel import select

from . import db
from .config import settings as app_settings
from .models import (
    AgentDef,
    Memory,
    MemoryKind,
    MemoryScope,
    ModelProvider,
    ProviderKind,
    SystemSetting,
)

log = logging.getLogger("agent_hq.memory")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "embedding_provider_id": None,
    "embedding_model": "",
    "recall_count": 6,
    "min_score": 0.25,
    "capture_delegations": True,
    "capture_events": True,
    "duplicate_threshold": 0.93,   # similarity above which two memories are the same thing
    "related_threshold": 0.75,     # similarity above which the tidy-up model reviews them together
    "tidy_provider_id": None,
    "tidy_model": "",
    "tidy_nightly": False,
    "tidy_hour": 3,
}

STOP = set("""a an and are as at be but by for from has have i in is it its of on or that the
this to was were will with what when where which who why how do does did can could should
would please any all my me we our you your there their them they then than so if not no""".split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- settings


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "memory")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    with db.session() as s:
        row = s.get(SystemSetting, "memory") or SystemSetting(key="memory")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


# --------------------------------------------------------------------------- embeddings


def _prefixed(model: str, text: str, is_query: bool) -> str:
    """Some embedding models expect task prefixes; using them noticeably helps retrieval."""
    m = model.lower()
    if "nomic" in m:
        return ("search_query: " if is_query else "search_document: ") + text
    if "qwen3-embedding" in m and is_query:
        return ("Instruct: Given a question, retrieve relevant notes from an IT team's memory\n"
                "Query: " + text)
    if ("mxbai" in m or "bge" in m) and is_query:
        return "Represent this sentence for searching relevant passages: " + text
    return text


async def embed_texts(texts: list[str], *, is_query: bool = False,
                      cfg: dict | None = None) -> tuple[list[list[float]], str] | None:
    """Embed with the configured provider. Returns (vectors, model) or None if not set up."""
    cfg = cfg or get_settings()
    model = cfg.get("embedding_model")
    if not model or not cfg.get("embedding_provider_id") or not texts:
        return None
    with db.session() as s:
        p = s.get(ModelProvider, cfg["embedding_provider_id"])
    if p is None or not p.enabled:
        return None
    inputs = [_prefixed(model, t, is_query) for t in texts]
    async with httpx.AsyncClient(timeout=60) as client:
        if p.kind == ProviderKind.ollama:
            from .llm import ollama_root

            r = await client.post(f"{ollama_root(p)}/api/embed", json={"model": model, "input": inputs})
            r.raise_for_status()
            vectors = r.json()["embeddings"]
        elif p.kind in (ProviderKind.openai, ProviderKind.openai_compatible):
            import os

            base = (p.base_url or "https://api.openai.com/v1").rstrip("/")
            key = p.api_key or (os.environ.get("OPENAI_API_KEY") if p.kind == ProviderKind.openai else None)
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = await client.post(f"{base}/embeddings", json={"model": model, "input": inputs}, headers=headers)
            r.raise_for_status()
            vectors = [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
        else:
            raise ValueError(f"{p.name} ({p.kind}) does not offer embeddings")
    return vectors, model


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_.:/-]*[a-z0-9]|[a-z0-9]", text.lower())
    return {w for w in words if w not in STOP}


def keyword_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    return len(query_tokens & tokens(text)) / len(query_tokens)


def recency(m: Memory) -> float:
    latest = max(_aware(m.last_confirmed), _aware(m.created_at))
    age_days = max((_now() - latest).total_seconds() / 86400, 0)
    return 0.5 ** (age_days / 60)


# --------------------------------------------------------------------------- pgvector


_pgvector: bool | None = None


def init_storage() -> None:
    """On Postgres: enable pgvector, add the vector column and a full-text index."""
    global _pgvector
    if not app_settings.is_postgres:
        _pgvector = False
        return
    try:
        with db.engine().begin() as c:
            c.execute(sql("CREATE EXTENSION IF NOT EXISTS vector"))
            c.execute(sql("ALTER TABLE memory ADD COLUMN IF NOT EXISTS embedding_vec vector"))
            c.execute(sql("CREATE INDEX IF NOT EXISTS memory_fts ON memory "
                          "USING gin (to_tsvector('english', text))"))
        _pgvector = True
    except Exception as e:
        log.warning("pgvector not available (%s); memory search falls back to Python", e)
        _pgvector = False


def pgvector_enabled() -> bool:
    if _pgvector is None:
        init_storage()
    return bool(_pgvector)


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _store_vector(memory_id: str, vector: list[float] | None, model: str | None) -> None:
    with db.session() as s:
        m = s.get(Memory, memory_id)
        m.embedding_model = model
        if pgvector_enabled():
            m.embedding = None
            s.add(m)
            s.commit()
            with db.engine().begin() as c:
                c.execute(sql("UPDATE memory SET embedding_vec = CAST(:v AS vector) WHERE id = :id"),
                          {"v": _vec_literal(vector) if vector else None, "id": memory_id})
        else:
            m.embedding = vector
            s.add(m)
            s.commit()


# --------------------------------------------------------------------------- service


@dataclass
class Hit:
    memory: Memory
    score: float
    similarity: float | None = None
    keywords: float = 0.0
    parts: dict = field(default_factory=dict)


def _scope_ok(m: Memory, agent_id: str | None, thread_id: str | None, everything: bool) -> bool:
    if everything:
        return True
    if m.scope == MemoryScope.team:
        return True
    if m.scope == MemoryScope.agent:
        return agent_id is not None and m.agent_id == agent_id
    return thread_id is not None and m.thread_id == thread_id


async def search(query: str, *, agent_id: str | None = None, thread_id: str | None = None,
                 k: int = 8, include_retired: bool = False, everything: bool = False,
                 min_score: float = 0.0) -> list[Hit]:
    """Hybrid search. `everything` ignores scopes (the Memory page)."""
    cfg = get_settings()
    qvec, model = None, None
    try:
        got = await embed_texts([query], is_query=True, cfg=cfg)
        if got:
            qvec, model = got[0][0], got[1]
    except Exception as e:
        log.warning("query embedding failed (%s); keyword search only", e)
    q_tokens = tokens(query)
    candidates: dict[str, tuple[Memory, float | None]] = {}

    if pgvector_enabled():
        retired = "" if include_retired else "AND invalidated_at IS NULL"
        with db.engine().connect() as c:
            if qvec is not None:
                rows = c.execute(sql(
                    f"SELECT id, 1 - (embedding_vec <=> CAST(:v AS vector)) AS sim FROM memory "
                    f"WHERE embedding_vec IS NOT NULL AND embedding_model = :m {retired} "
                    f"ORDER BY embedding_vec <=> CAST(:v AS vector) LIMIT 60"),
                    {"v": _vec_literal(qvec), "m": model}).fetchall()
                sims = {r[0]: float(r[1]) for r in rows}
            else:
                sims = {}
            words = " OR ".join(sorted(q_tokens)) or query
            fts = c.execute(sql(
                f"SELECT id FROM memory WHERE to_tsvector('english', text) @@ "
                f"websearch_to_tsquery('english', :q) {retired} LIMIT 60"), {"q": words}).fetchall()
            ids = set(sims) | {r[0] for r in fts}
            if q_tokens:  # identifiers like pve1 or 10.0.0.5 that full-text may not index well
                like = c.execute(sql(f"SELECT id FROM memory WHERE ({' OR '.join(f'text ILIKE :t{i}' for i in range(len(q_tokens)))}) {retired} LIMIT 60"),
                                 {f"t{i}": f"%{t}%" for i, t in enumerate(sorted(q_tokens))}).fetchall()
                ids |= {r[0] for r in like}
        with db.session() as s:
            for mid in ids:
                m = s.get(Memory, mid)
                if m:
                    candidates[mid] = (m, sims.get(mid))
    else:
        with db.session() as s:
            q = select(Memory)
            if not include_retired:
                q = q.where(Memory.invalidated_at == None)  # noqa: E711
            for m in s.exec(q.order_by(Memory.created_at.desc()).limit(5000)).all():
                sim = cosine(qvec, m.embedding) if qvec is not None and m.embedding and m.embedding_model == model else None
                candidates[m.id] = (m, sim)

    hits = []
    for m, sim in candidates.values():
        if not _scope_ok(m, agent_id, thread_id, everything):
            continue
        kw = keyword_score(q_tokens, m.text)
        rec = recency(m)
        if qvec is not None:
            score = 0.65 * max(sim or 0.0, 0.0) + 0.25 * kw + 0.10 * rec
        else:
            score = 0.80 * kw + 0.20 * rec if kw > 0 else 0.0
        if m.invalidated_at is not None:
            score *= 0.6  # retired memories (shown only on request) rank below current ones
        if score >= min_score:
            hits.append(Hit(m, score, sim, kw, {"recency": rec}))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:k]


async def add(text: str, *, kind: MemoryKind | str = MemoryKind.fact,
              scope: MemoryScope | str = MemoryScope.team, agent_id: str | None = None,
              thread_id: str | None = None, source: str = "user", source_job_id: str | None = None,
              created_by: str | None = None, supersedes: str | None = None,
              meta: dict | None = None) -> Memory:
    text = text.strip()[:4000]
    if not text:
        raise ValueError("memory text is empty")
    m = Memory(text=text, kind=MemoryKind(kind), scope=MemoryScope(scope), agent_id=agent_id,
               thread_id=thread_id, source=source, source_job_id=source_job_id,
               created_by=created_by, meta=meta or {})
    with db.session() as s:
        s.add(m)
        s.commit()
        s.refresh(m)
    try:
        got = await embed_texts([text])
        if got:
            _store_vector(m.id, got[0][0], got[1])
    except Exception as e:
        log.warning("embedding failed for memory %s (%s); it will be keyword-searchable only "
                    "until re-embedded", m.id, e)
    if supersedes:
        retire(supersedes, f"superseded by {m.id}", superseded_by=m.id)
    return get(m.id)


def get(memory_id: str) -> Memory | None:
    with db.session() as s:
        return s.get(Memory, memory_id)


def update(memory_id: str, **fields: Any) -> Memory:
    with db.session() as s:
        m = s.get(Memory, memory_id)
        if m is None:
            raise LookupError(memory_id)
        for k, v in fields.items():
            setattr(m, k, v)
        m.updated_at = _now()
        s.add(m)
        s.commit()
        s.refresh(m)
        return m


def retire(memory_id: str, reason: str, superseded_by: str | None = None) -> Memory:
    return update(memory_id, invalidated_at=_now(), invalidated_reason=reason[:500],
                  superseded_by=superseded_by)


def restore(memory_id: str) -> Memory:
    return update(memory_id, invalidated_at=None, invalidated_reason=None, superseded_by=None)


def confirm(memory_id: str) -> Memory:
    m = get(memory_id)
    return update(memory_id, last_confirmed=_now(), times_seen=(m.times_seen if m else 0) + 1)


async def capture(text: str, **kw: Any) -> Memory:
    """Add automatically-captured memory, unless a near-duplicate exists (then confirm it)."""
    cfg = get_settings()
    scope = MemoryScope(kw.get("scope", MemoryScope.team))
    hits = await search(text, agent_id=kw.get("agent_id"), thread_id=kw.get("thread_id"), k=3)
    for h in hits:
        same_scope = h.memory.scope == scope
        near = (h.similarity is not None and h.similarity >= cfg["duplicate_threshold"]) or \
               (h.similarity is None and h.memory.text.strip().lower() == text.strip().lower())
        if same_scope and near:
            return confirm(h.memory.id)
    return await add(text, **kw)


def format_recall(hits: list[Hit]) -> str:
    if not hits:
        return ""
    lines = []
    for h in hits:
        m = h.memory
        date = _aware(m.last_confirmed).strftime("%Y-%m-%d")
        lines.append(f"- [{m.id} · {date} · {m.kind.value} · {m.scope.value}] {m.text}")
    return ("Relevant memories from past work (they may be out of date: verify anything "
            "important before acting on it; use `forget` to retire one that is wrong, "
            "`confirm` when you verify one):\n" + "\n".join(lines))


async def recall_for(agent: AgentDef, query: str, thread_id: str | None) -> str:
    cfg = get_settings()
    if not cfg["enabled"] or not agent.memory_enabled or not query.strip():
        return ""
    hits = await search(query[:2000], agent_id=agent.id, thread_id=thread_id,
                        k=int(cfg["recall_count"]), min_score=float(cfg["min_score"]))
    return format_recall(hits)


# --------------------------------------------------------------------------- maintenance


async def reembed(batch: int = 32) -> dict:
    """Embed every memory that has no embedding from the current model."""
    cfg = get_settings()
    if not cfg["embedding_model"] or not cfg["embedding_provider_id"]:
        return {"embedded": 0, "reason": "no embedding model configured"}
    with db.session() as s:
        todo = [m for m in s.exec(select(Memory)).all() if m.embedding_model != cfg["embedding_model"]
                or (not pgvector_enabled() and not m.embedding)]
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        got = await embed_texts([m.text for m in chunk], cfg=cfg)
        if not got:
            break
        for m, v in zip(chunk, got[0]):
            _store_vector(m.id, v, got[1])
            done += 1
    return {"embedded": done}


def _vectors(memories: list[Memory]) -> dict[str, list[float]]:
    if not pgvector_enabled():
        return {m.id: m.embedding for m in memories if m.embedding}
    if not memories:
        return {}
    with db.engine().connect() as c:
        rows = c.execute(sql("SELECT id, embedding_vec::text FROM memory WHERE id = ANY(:ids) "
                             "AND embedding_vec IS NOT NULL"), {"ids": [m.id for m in memories]}).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


TIDY_PROMPT = """You maintain the long-term memory of an IT operations team of AI agents.
These memories look related. Decide what the team should keep.

{items}

Reply with JSON only, in this shape:
{{"actions": [
  {{"type": "merge", "ids": ["id1", "id2"], "text": "one combined memory"}},
  {{"type": "retire", "id": "id3", "reason": "why it is outdated or wrong"}}
]}}
Use "merge" for duplicates or memories that belong together. Use "retire" when a newer memory
shows an older one is no longer true. Leave memories you would keep as-is out of the list."""


def _same_audience(a: Memory, b: Memory) -> bool:
    """Whether two memories are visible to the same agents, so they can be merged.

    Only the scope's own key counts: a team memory captured in one conversation is the same
    team memory as one captured in another (thread_id there just records where it came
    from), while agent memories must belong to the same agent and conversation memories to
    the same conversation.
    """
    if a.scope != b.scope:
        return False
    if a.scope == MemoryScope.agent:
        return a.agent_id == b.agent_id
    if a.scope == MemoryScope.thread:
        return a.thread_id == b.thread_id
    return True


async def tidy(use_model: bool = True) -> dict:
    """Group near-duplicate memories; merge or retire with a model, or dedupe without one."""
    cfg = get_settings()
    with db.session() as s:
        live = list(s.exec(select(Memory).where(Memory.invalidated_at == None)).all())  # noqa: E711
    vecs = _vectors(live)
    threshold = float(cfg["duplicate_threshold"])
    llm_threshold = float(cfg["related_threshold"])  # looser grouping when a model judges the group
    have_model = use_model and bool(cfg.get("tidy_model"))
    group_at = llm_threshold if have_model else threshold
    used: set[str] = set()
    groups: list[list[Memory]] = []
    for m in sorted(live, key=lambda x: _aware(x.created_at)):
        if m.id in used or m.id not in vecs:
            continue
        group = [m]
        for o in live:
            if o.id == m.id or o.id in used or o.id not in vecs:
                continue
            if _same_audience(m, o) and cosine(vecs[m.id], vecs[o.id]) >= group_at:
                group.append(o)
        if len(group) > 1:
            used.update(x.id for x in group)
            groups.append(group)

    report = {"groups": len(groups), "merged": 0, "retired": 0, "model_used": have_model}
    for group in groups:
        group.sort(key=lambda x: _aware(x.created_at))
        if have_model:
            try:
                actions = await _ask_tidy_model(group, cfg)
                await _apply(actions, group, report)
                continue
            except Exception as e:
                log.warning("tidy model failed (%s); falling back to duplicate removal", e)
        newest = group[-1]
        for old in group[:-1]:
            if cosine(vecs[old.id], vecs[newest.id]) >= threshold:
                retire(old.id, "duplicate", superseded_by=newest.id)
                report["retired"] += 1
        confirm(newest.id)
    return report


async def _ask_tidy_model(group: list[Memory], cfg: dict) -> list[dict]:
    from pydantic_ai import Agent

    from .llm import build_model_for

    items = "\n".join(f"- id={m.id} date={_aware(m.created_at):%Y-%m-%d} kind={m.kind.value}: {m.text}"
                      for m in group)
    model = build_model_for(cfg.get("tidy_provider_id"), cfg["tidy_model"], "memory tidy-up")
    result = await Agent(model, output_type=str).run(TIDY_PROMPT.format(items=items))
    raw = str(result.output)
    match = re.search(r"\{.*\}", raw, re.S)
    return json.loads(match.group(0)).get("actions", []) if match else []


async def _apply(actions: list[dict], group: list[Memory], report: dict) -> None:
    ids = {m.id for m in group}
    by_id = {m.id: m for m in group}
    for a in actions:
        if a.get("type") == "merge":
            merged_ids = [i for i in a.get("ids", []) if i in ids]
            if len(merged_ids) < 2 or not a.get("text"):
                continue
            first = by_id[merged_ids[-1]]
            new = await add(a["text"], kind=first.kind, scope=first.scope, agent_id=first.agent_id,
                            thread_id=first.thread_id, source="tidy",
                            meta={"merged_from": merged_ids})
            for i in merged_ids:
                retire(i, "merged", superseded_by=new.id)
            report["merged"] += 1
        elif a.get("type") == "retire" and a.get("id") in ids:
            retire(a["id"], a.get("reason") or "outdated")
            report["retired"] += 1
