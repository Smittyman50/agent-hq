"""Sub-agents: temporary agents an agent creates for part of its own task.

Who may create them is set per agent (Settings → Agents → Sub-agents):

* **off** (the default): no sub-agent tools at all.
* **ask**: the agent gets `request_subagents`, a request to you saying which sub-agents
  it wants, why, why none of the existing agents fits, and how much (the system adds its
  own estimate: models, sizes, where they run, averages from the usage ledger). You
  approve (optionally with changes) or deny it. An approval is a time-limited grant for
  that agent in that conversation; `spawn_subagent` works only within it.
* **allowed**: the agent may spawn within the limits set on it.

Every spawn is checked atomically (under a Valkey lock, so several workers can't race
past a limit) against the agent's or grant's limits and the team-wide caps, and refused
when the sub-agent would duplicate an agent you have already defined: a sub-agent's role
may not reuse an agent's name, the agent has to say why no existing agent fits, and when
an embedding model is set (Settings → Memory) the role is compared with every agent's
description. Each sub-agent run is capped in model requests and minutes, and the
sub-agent retires as soon as it reports back. Sub-agents can't delegate or spawn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlmodel import func, select

from . import db, memory
from .config import settings
from .models import (
    AgentDef,
    Job,
    JobStatus,
    ModelProvider,
    ProviderKind,
    SubagentMode,
    SubagentRequest,
    SystemSetting,
    ToolServer,
    UsageRecord,
    default_subagent_limits,
)

log = logging.getLogger("agent_hq.subagents")

DEFAULTS: dict[str, Any] = {
    "max_live": 4,              # sub-agents alive at once, across the whole team
    "max_per_day": 20,          # sub-agents created per day (UTC), across the whole team
    "similarity_threshold": 0.78,  # role vs. an existing agent's description: above = refused
    "max_pending_per_agent": 2,    # unanswered requests an agent may have open
    "request_ttl_hours": 24,       # an unanswered request lapses after this long
    "notify_mqtt": True,           # publish requests and decisions on AHQ_MQTT_NOTIFY_TOPIC
}

GRACE_MINUTES = 5  # time to get a worker on top of a sub-agent's max_minutes


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def start_of_day() -> datetime:
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------- settings


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "subagents")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    with db.session() as s:
        row = s.get(SystemSetting, "subagents") or SystemSetting(key="subagents")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


def limits_of(agent: AgentDef) -> dict[str, Any]:
    return {**default_subagent_limits(), **(agent.subagent_limits or {})}


def mode_of(agent: AgentDef) -> SubagentMode:
    if agent.is_subagent:
        return SubagentMode.off  # sub-agents never nest
    try:
        return SubagentMode(agent.subagent_mode or "off")
    except ValueError:
        return SubagentMode.off


# --------------------------------------------------------------------------- options


def _provider(pid: str | None) -> ModelProvider | None:
    if not pid:
        return None
    with db.session() as s:
        return s.get(ModelProvider, pid)


def model_label(provider_id: str | None, model: str) -> str:
    p = _provider(provider_id)
    return f"{model} @ {p.name}" if p else model


def model_options(agent: AgentDef, limits: dict[str, Any]) -> list[dict[str, Any]]:
    """[{provider_id, model, label}] a sub-agent of this agent may run on."""
    chosen = limits.get("models") or [{"provider_id": agent.provider_id, "model": agent.model}]
    out = []
    for m in chosen:
        if not m.get("model"):
            continue
        out.append({"provider_id": m.get("provider_id"), "model": m["model"],
                    "label": model_label(m.get("provider_id"), m["model"])})
    return out


def pick_model(options: list[dict[str, Any]], wanted: str | None) -> dict[str, Any] | None:
    if not options:
        return None
    if not wanted or not wanted.strip():
        return options[0]
    w = wanted.strip().lower()
    for o in options:
        if w in (o["label"].lower(), o["model"].lower()):
            return o
    return None


def tool_options(agent: AgentDef, limits: dict[str, Any]) -> list[ToolServer]:
    """Tool servers a sub-agent may use: the agent's own, minus desktops (one desktop
    can't be driven by two agents at once), narrowed by the limits."""
    from .runtime import agent_tool_servers

    own = [srv for srv in agent_tool_servers(agent) if not srv.desktop]
    allowed = limits.get("tool_server_ids")
    if allowed is None:
        return own
    return [srv for srv in own if srv.id in allowed]


def pick_tools(options: list[ToolServer], wanted: list[str] | None) -> tuple[list[ToolServer], list[str]]:
    """(chosen servers, names that aren't allowed). None = all allowed ones."""
    if wanted is None:
        return options, []
    by_name = {srv.name.lower(): srv for srv in options}
    chosen, bad = [], []
    for n in wanted:
        srv = by_name.get(str(n).strip().lower())
        (chosen.append(srv) if srv else bad.append(str(n)))
    return chosen, bad


# --------------------------------------------------------------------------- duplicates


def defined_agents(exclude_id: str | None = None) -> list[AgentDef]:
    with db.session() as s:
        rows = s.exec(select(AgentDef).where(AgentDef.enabled == True,  # noqa: E712
                                             AgentDef.is_subagent == False)).all()  # noqa: E712
    return [a for a in rows if a.id != exclude_id]


_desc_cache: dict[tuple[str, str], list[float]] = {}


async def _embed(texts: list[str], is_query: bool) -> list[list[float]] | None:
    """Embeddings from the memory settings' embedding model, or None if none is set."""
    got = await memory.embed_texts(texts, is_query=is_query)
    return got[0] if got else None


async def overlapping_agents(text: str, *, exclude_id: str | None = None) -> list[dict[str, Any]]:
    """Defined agents whose description is close to `text`: [{name, id, score}], best first.

    The asking agent itself is excluded: splitting its own work across helpers is the
    point of sub-agents. Empty when no embedding model is configured.
    """
    agents = [a for a in defined_agents(exclude_id) if (a.description or "").strip()]
    if not agents or not text.strip():
        return []
    try:
        q = await _embed([text], True)
        if not q:
            return []
        missing = [a for a in agents if (a.id, a.description) not in _desc_cache]
        if missing:
            vecs = await _embed([a.description for a in missing], False) or []
            for a, v in zip(missing, vecs):
                _desc_cache[(a.id, a.description)] = v
    except Exception as e:  # never let the check itself break the agent
        log.warning("sub-agent overlap check failed: %s", e)
        return []
    hits = [{"id": a.id, "name": a.name,
             "score": round(memory.cosine(q[0], _desc_cache.get((a.id, a.description), [])), 3)}
            for a in agents]
    return sorted(hits, key=lambda h: -h["score"])


def name_clash(role: str, exclude_id: str | None = None) -> AgentDef | None:
    key = role.strip().lower()
    for a in defined_agents(exclude_id):
        if a.name.lower() == key or (len(a.name) > 2 and re.search(rf"\b{re.escape(a.name.lower())}\b", key)):
            return a
    return None


async def duplicate_of(role: str, purpose: str, me: AgentDef) -> tuple[AgentDef | None, float]:
    """The defined agent a proposed sub-agent duplicates, if any (with the score)."""
    clash = name_clash(role, me.id)
    if clash:
        return clash, 1.0
    threshold = float(get_settings()["similarity_threshold"])
    hits = await overlapping_agents(f"{role}: {purpose}", exclude_id=me.id)
    if hits and hits[0]["score"] >= threshold:
        with db.session() as s:
            return s.get(AgentDef, hits[0]["id"]), hits[0]["score"]
    return None, hits[0]["score"] if hits else 0.0


# --------------------------------------------------------------------------- counting


def counts(parent_id: str, job_id: str | None) -> dict[str, int]:
    """Live and recent sub-agent counts used by the limit checks."""
    today = start_of_day()
    with db.session() as s:
        def n(*where) -> int:
            return s.exec(select(func.count()).select_from(AgentDef)
                          .where(AgentDef.is_subagent == True, *where)).one()  # noqa: E712

        return {
            "live": n(AgentDef.parent_agent_id == parent_id, AgentDef.retired == False),  # noqa: E712
            "task": n(AgentDef.spawned_by_job_id == job_id) if job_id else 0,
            "today": n(AgentDef.parent_agent_id == parent_id, AgentDef.created_at >= today),
            "team_live": n(AgentDef.retired == False),  # noqa: E712
            "team_today": n(AgentDef.created_at >= today),
        }


def check_limits(c: dict[str, int], limits: dict[str, Any], team: dict[str, Any],
                 grant_left: int | None = None) -> str | None:
    """Why another sub-agent can't be created now, or None if it can."""
    if c["team_live"] >= int(team["max_live"]):
        return f"the team already has {c['team_live']} sub-agents running (team limit {team['max_live']})"
    if c["team_today"] >= int(team["max_per_day"]):
        return f"the team has used today's {team['max_per_day']} sub-agents"
    if c["live"] >= int(limits["concurrent"]):
        return f"you already have {c['live']} sub-agent(s) running (your limit is {limits['concurrent']} at once)"
    if grant_left is not None:
        if grant_left <= 0:
            return "your approved sub-agents are used up; ask again with request_subagents if you need more"
        return None
    if c["task"] >= int(limits["per_task"]):
        return f"you've created {c['task']} sub-agent(s) for this task (limit {limits['per_task']})"
    if c["today"] >= int(limits["per_day"]):
        return f"you've used today's {limits['per_day']} sub-agents"
    return None


# --------------------------------------------------------------------------- grants


def active_grant(agent_id: str, thread_id: str) -> SubagentRequest | None:
    now = _now()
    with db.session() as s:
        rows = s.exec(select(SubagentRequest).where(
            SubagentRequest.agent_id == agent_id, SubagentRequest.thread_id == thread_id,
            SubagentRequest.status == "approved").order_by(SubagentRequest.decided_at.desc())).all()
    for r in rows:
        if aware(r.expires_at) and aware(r.expires_at) < now:
            continue
        if r.used < int((r.grant or {}).get("count", 0)):
            return r
    return None


def grant_limits(req: SubagentRequest) -> dict[str, Any]:
    g = req.grant or {}
    return {**default_subagent_limits(), "concurrent": g.get("concurrent", 1),
            "per_task": g.get("count", 1), "per_day": g.get("count", 1),
            "max_minutes": g.get("max_minutes", 10), "max_requests": g.get("max_requests", 25),
            "models": g.get("models") or [], "tool_server_ids": g.get("tool_server_ids")}


def pending_count(agent_id: str) -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(SubagentRequest).where(
            SubagentRequest.agent_id == agent_id, SubagentRequest.status == "pending")).one()


# --------------------------------------------------------------------------- ledger


def record_usage(agent: AgentDef, *, provider_id: str | None, model: str, usage: Any,
                 seconds: float, ok: bool, job_id: str | None, thread_id: str | None) -> None:
    p = _provider(provider_id)
    row = UsageRecord(
        agent_id=agent.id, agent_name=agent.name, is_subagent=agent.is_subagent,
        parent_agent_id=agent.parent_agent_id, job_id=job_id, thread_id=thread_id,
        provider_id=provider_id, provider_name=p.name if p else "", model=model or "",
        requests=int(getattr(usage, "requests", 0) or 0),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        seconds=round(seconds, 2), ok=ok)
    try:
        with db.session() as s:
            s.add(row)
            s.commit()
    except Exception as e:  # the ledger must never break a run
        log.warning("could not record usage: %s", e)


def model_averages(provider_id: str | None, model: str, days: int = 30) -> dict[str, Any]:
    since = _now() - timedelta(days=days)
    with db.session() as s:
        rows = s.exec(select(UsageRecord).where(UsageRecord.provider_id == provider_id,
                                                UsageRecord.model == model,
                                                UsageRecord.at >= since)).all()
    if not rows:
        return {"runs": 0}
    n = len(rows)
    return {"runs": n,
            "avg_seconds": round(sum(r.seconds for r in rows) / n, 1),
            "avg_requests": round(sum(r.requests for r in rows) / n, 1),
            "avg_tokens": round(sum(r.input_tokens + r.output_tokens for r in rows) / n),
            "days": days}


# --------------------------------------------------------------------------- estimates


def where_it_runs(p: ModelProvider | None) -> str:
    if p is None:
        return "raw model spec (no provider)"
    if p.kind in (ProviderKind.ollama, ProviderKind.openai_compatible):
        host = urlparse(p.base_url or "http://localhost").hostname or "local"
        return f"your hardware ({host}: GPU/CPU)"
    if p.kind == ProviderKind.chatgpt:
        return "your ChatGPT plan (counts against its usage limits)"
    return f"cloud API ({p.kind.value}, billed per token)"


async def _model_details(p: ModelProvider | None, model: str) -> str:
    if p is None:
        return ""
    from .llm import list_models

    try:
        listed = await asyncio.wait_for(list_models(p), 5)
    except Exception:
        return ""
    for m in listed:
        if m.get("id") == model:
            return m.get("details", "")
    return ""


async def estimate(me: AgentDef, plans: list[dict[str, Any]], *, concurrent: int) -> dict[str, Any]:
    """The system's own view of what a request costs, shown next to the agent's case."""
    from . import slots

    items, overlaps = [], []
    for plan in plans:
        p = _provider(plan.get("provider_id"))
        avg = model_averages(plan.get("provider_id"), plan.get("model", ""))
        free = None
        if p and p.max_concurrent:
            free = max(0, p.max_concurrent - await slots.in_use(p.id))
        items.append({
            "role": plan["role"], "model": plan.get("label") or plan.get("model"),
            "model_details": await _model_details(p, plan.get("model", "")),
            "runs_on": where_it_runs(p), "est_minutes": plan.get("est_minutes", 10),
            "provider_slots": p.max_concurrent if p else 0, "slots_free_now": free,
            "ledger": avg})
        hits = await overlapping_agents(f"{plan['role']}: {plan.get('purpose', '')}", exclude_id=me.id)
        if hits and hits[0]["score"] >= 0.6:
            overlaps.append({"role": plan["role"], "closest_agent": hits[0]["name"],
                             "score": hits[0]["score"]})
    total = sum(int(i["est_minutes"] or 0) for i in items)
    return {"subagents": items, "total_minutes": total, "peak_concurrent": min(concurrent, len(items)),
            "wall_minutes": round(total / max(1, min(concurrent, len(items))), 1) if items else 0,
            "overlaps": overlaps,
            "overlap_check": bool(memory.get_settings().get("embedding_model"))}


# --------------------------------------------------------------------------- upkeep


def sweep() -> dict[str, int]:
    """Retire expired sub-agents and lapse old requests and grants."""
    now = _now()
    out = {"retired": 0, "requests": 0}
    with db.session() as s:
        for a in s.exec(select(AgentDef).where(AgentDef.is_subagent == True,  # noqa: E712
                                               AgentDef.retired == False)).all():  # noqa: E712
            job = s.exec(select(Job).where(Job.assignee_agent_id == a.id)).first()
            done = job is not None and job.status in (JobStatus.done, JobStatus.failed)
            if done or (aware(a.expires_at) and aware(a.expires_at) < now):
                a.retired, a.enabled = True, False
                s.add(a)
                out["retired"] += 1
        for r in s.exec(select(SubagentRequest).where(
                SubagentRequest.status.in_(("pending", "approved")))).all():
            if aware(r.expires_at) and aware(r.expires_at) < now:
                r.status = "expired"
                s.add(r)
                out["requests"] += 1
        s.commit()
    return out


def retire(agent_id: str) -> None:
    with db.session() as s:
        a = s.get(AgentDef, agent_id)
        if a and a.is_subagent and not a.retired:
            a.retired, a.enabled = True, False
            s.add(a)
            s.commit()


# --------------------------------------------------------------------------- notifications


def link(path: str) -> str | None:
    return f"{settings.public_url}{path}" if settings.public_url else None


async def notify(event: dict[str, Any]) -> bool:
    """Publish a notification for you on MQTT (in addition to the in-app card). Best effort."""
    if not settings.mqtt_url or not get_settings().get("notify_mqtt", True):
        return False
    import uuid

    import aiomqtt

    from .mqtt_bridge import client_args

    args = client_args(settings.mqtt_url)
    # A client ID of its own: reusing the bridge's would kick the bridge off the broker.
    args["identifier"] = f"agent-hq-notify-{settings.worker_name}-{uuid.uuid4().hex[:6]}"
    payload = json.dumps({**event, "at": _now().isoformat()}, default=str)
    try:
        async with asyncio.timeout(10):
            async with aiomqtt.Client(**args) as client:
                await client.publish(settings.mqtt_notify_topic, payload, qos=1)
        return True
    except Exception as e:
        log.warning("MQTT notification failed: %s", e)
        return False
