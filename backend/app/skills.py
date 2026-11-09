"""Skills: written-down know-how that agents use and improve.

A skill is a short procedure with a name, *when to use it*, the steps (markdown), the tools
it relies on and, optionally, a saved workbench script that does the mechanical part.
Skills are shared by the whole team or belong to one agent, and every change is a new
version you can roll back.

How agents use them:

* Each run, the agent sees the names and one-line "when to use" of the skills that match
  the task (by meaning, when an embedding model is set in Settings → Memory; by keywords
  otherwise), and loads the full procedure with `load_skill` only when it needs it.
* Each load is recorded with the job's outcome (done / partial / failed), so a skill's
  success rate shows on the Skills page.
* Agents propose new skills or improvements with `propose_skill`. Per agent (Settings →
  Agents → Learning): **propose** (the default) sends the proposal to your Requests inbox
  to approve, edit or reject; **auto** applies it at once and tells you; **off** means
  the agent can use skills but not propose them.

Skills are instructions, so they never grant tools: approvals still apply to every call
a skill leads to.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlmodel import func, or_, select

from . import db, memory
from .models import AgentDef, LearningMode, Skill, SkillProposal, SkillUse, SkillVersion

log = logging.getLogger("agent_hq.skills")

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,60}$")
LIST_LIMIT = 8          # skills listed in an agent's instructions per run
LIST_ALL_UNDER = 12     # with this few skills, list them all (no matching needed)
MIN_MATCH = 0.3         # embedding similarity for a skill to be listed

PROPOSE_RULES = """- When you work out how to do something that will come up again (a procedure, a
  workaround, a pitfall), or a skill you used was wrong or incomplete, call
  `propose_skill` once, near the end: name it, say when to use it, write the steps
  someone else could follow, and why. To improve an existing skill, pass its name as
  `revise`. Don't propose one-off facts (those are memories) or trivial steps."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def mode_of(agent: AgentDef) -> LearningMode:
    if agent.is_subagent:
        return LearningMode.off  # sub-agents use skills but don't write them
    try:
        return LearningMode(agent.learning_mode or "propose")
    except ValueError:
        return LearningMode.propose


def visible_to(agent: AgentDef) -> list[Skill]:
    """Active skills this agent can use: team skills and its own."""
    owner = agent.parent_agent_id if agent.is_subagent else agent.id
    with db.session() as s:
        return list(s.exec(select(Skill).where(
            Skill.status == "active",
            or_(Skill.scope == "team", Skill.agent_id == owner)).order_by(Skill.name)).all())


def find(name: str, agent: AgentDef | None = None) -> Skill | None:
    key = (name or "").strip().lower()
    pool = visible_to(agent) if agent else None
    if pool is not None:
        return next((k for k in pool if k.name == key), None)
    with db.session() as s:
        return s.exec(select(Skill).where(Skill.name == key)).first()


# --------------------------------------------------------------------------- matching

_vec_cache: dict[tuple[str, int], list[float]] = {}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}


async def relevant(agent: AgentDef, task: str, limit: int = LIST_LIMIT) -> list[Skill]:
    """The skills worth listing for this task."""
    pool = visible_to(agent)
    if len(pool) <= min(limit, LIST_ALL_UNDER):
        return pool
    def text(k: Skill) -> str:
        return f"{k.title}. {k.when_to_use}"
    try:
        q = await memory.embed_texts([task], is_query=True)
        if q and q[0]:
            missing = [k for k in pool if (k.id, k.version) not in _vec_cache]
            if missing:
                got = await memory.embed_texts([text(k) for k in missing], is_query=False)
                for k, v in zip(missing, (got[0] if got else [])):
                    _vec_cache[(k.id, k.version)] = v
            scored = sorted(((memory.cosine(q[0][0], _vec_cache.get((k.id, k.version), [])), k)
                             for k in pool), key=lambda x: -x[0])
            return [k for score, k in scored[:limit] if score >= MIN_MATCH]
    except Exception as e:  # matching must never stop the agent
        log.warning("skill matching by meaning failed, using keywords: %s", e)
    words = _words(task)
    scored = sorted(((len(words & _words(text(k) + " " + k.name.replace("_", " "))), k) for k in pool),
                    key=lambda x: -x[0])
    return [k for score, k in scored[:limit] if score > 0]


def listing(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {k.name}: {k.title} (use when: {k.when_to_use.strip()[:200]})" for k in skills]
    return "Skills that may fit:\n" + "\n".join(lines)


# --------------------------------------------------------------------------- use and outcomes


def record_use(skill: Skill, agent_id: str, job_id: str | None, thread_id: str | None) -> None:
    with db.session() as s:
        s.add(SkillUse(skill_id=skill.id, version=skill.version, agent_id=agent_id,
                       job_id=job_id, thread_id=thread_id))
        s.commit()


def finish_job(job_id: str | None, outcome: str) -> None:
    """Record how a job that used skills turned out."""
    if not job_id:
        return
    outcome = outcome if outcome in ("done", "partial", "failed") else "failed"
    try:
        with db.session() as s:
            for u in s.exec(select(SkillUse).where(SkillUse.job_id == job_id,
                                                   SkillUse.outcome == "pending")).all():
                u.outcome = outcome
                s.add(u)
            s.commit()
    except Exception as e:
        log.warning("could not record skill outcomes: %s", e)


def stats(skill_ids: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """{skill id: {uses, done, partial, failed, last_used_at}}"""
    with db.session() as s:
        q = select(SkillUse)
        if skill_ids is not None:
            q = q.where(SkillUse.skill_id.in_(skill_ids))
        rows = s.exec(q).all()
    out: dict[str, dict[str, Any]] = {}
    for u in rows:
        g = out.setdefault(u.skill_id, {"uses": 0, "done": 0, "partial": 0, "failed": 0,
                                        "pending": 0, "last_used_at": None})
        g["uses"] += 1
        g[u.outcome] = g.get(u.outcome, 0) + 1
        if g["last_used_at"] is None or u.at > g["last_used_at"]:
            g["last_used_at"] = u.at
    return out


# --------------------------------------------------------------------------- changes


def check_fields(name: str, title: str, when_to_use: str, body: str) -> str | None:
    if not NAME_RE.match(name or ""):
        return "name must be lowercase letters, digits and underscores, e.g. check_pve_backups"
    if not title.strip() or len(title) > 120:
        return "give it a short title (up to 120 characters)"
    if len(when_to_use.strip()) < 10:
        return "say when to use it (a sentence)"
    if len(body.strip()) < 20:
        return "write the steps (the procedure itself)"
    if len(body) > 20000:
        return "keep the procedure under 20,000 characters"
    return None


def create(*, name: str, title: str, when_to_use: str, body: str, tools: list[str] | None = None,
           script: str | None = None, scope: str = "team", agent_id: str | None = None,
           created_by: str = "", note: str = "") -> Skill:
    k = Skill(name=name, title=title.strip(), when_to_use=when_to_use.strip(), body=body.strip(),
              tools=list(tools or []), script=(script or None), scope=scope,
              agent_id=agent_id if scope == "agent" else None, created_by=created_by)
    with db.session() as s:
        s.add(k)
        s.add(SkillVersion(skill_id=k.id, version=1, title=k.title, when_to_use=k.when_to_use,
                           body=k.body, tools=k.tools, script=k.script, created_by=created_by,
                           note=note or "created"))
        s.commit()
        s.refresh(k)
    return k


def revise(skill_id: str, *, title: str, when_to_use: str, body: str, tools: list[str] | None,
           script: str | None, created_by: str, note: str = "") -> Skill:
    with db.session() as s:
        k = s.get(Skill, skill_id)
        k.version += 1
        k.title, k.when_to_use, k.body = title.strip(), when_to_use.strip(), body.strip()
        k.tools, k.script, k.updated_at = list(tools or []), (script or None), _now()
        s.add(k)
        s.add(SkillVersion(skill_id=k.id, version=k.version, title=k.title,
                           when_to_use=k.when_to_use, body=k.body, tools=k.tools,
                           script=k.script, created_by=created_by, note=note))
        s.commit()
        s.refresh(k)
    return k


def rollback(skill_id: str, version: int, by: str) -> Skill:
    with db.session() as s:
        v = s.exec(select(SkillVersion).where(SkillVersion.skill_id == skill_id,
                                              SkillVersion.version == version)).first()
    if v is None:
        raise LookupError("no such version")
    return revise(skill_id, title=v.title, when_to_use=v.when_to_use, body=v.body, tools=v.tools,
                  script=v.script, created_by=by, note=f"rolled back to version {version}")


def diff(old: str, new: str) -> str:
    return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "current", "proposed",
                                         lineterm="", n=2))


def apply_proposal(p: SkillProposal, by: str) -> Skill:
    """Turn a proposal into a new skill or a new version of one."""
    note = f"proposed by {p.agent_name}: {p.reason}".strip()[:500]
    if p.skill_id:
        return revise(p.skill_id, title=p.title, when_to_use=p.when_to_use, body=p.body,
                      tools=p.tools, script=p.script, created_by=by, note=note)
    if find(p.name) is not None:
        raise ValueError(f"a skill named {p.name} already exists")
    return create(name=p.name, title=p.title, when_to_use=p.when_to_use, body=p.body, tools=p.tools,
                  script=p.script, scope=p.scope, agent_id=p.agent_id, created_by=by, note=note)


def pending_count() -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(SkillProposal)
                      .where(SkillProposal.status == "pending")).one()
