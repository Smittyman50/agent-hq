"""The learning loop: agents look back at finished work and keep what's worth keeping.

After a job, an agent may *reflect*: it rereads the conversation, the tool calls it made,
how the job turned out and what you said about it, then proposes new or better skills
(sent to Requests → Skill proposals, or applied at once, per its learning mode) and saves
short lessons to its memory ("Mike wants the backup report as a table, failures first").

A job gets a look when there's a reason to (Settings → Skills → Learning):

* you rated the reply 👎 (with a note on what it should have done, ideally) or 👍 on work
  that took several steps;
* you corrected the agent in the conversation ("no, use the TrueNAS datastore");
* the job failed or only partly worked;
* it took many tool calls (work worth writing down as a procedure);
* or after every job, if you choose that.

A reflection waits a few minutes after the job (so your follow-ups are included), runs on
the agent's own model unless you pick a reflection model, and is capped per day. Agents
whose learning mode is Off, and sub-agents, never reflect.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlmodel import func, select, update

from . import db, memory, skills, subagents
from .models import (
    AgentDef,
    Feedback,
    Job,
    LearningMode,
    MemoryKind,
    MemoryScope,
    Message,
    Reflection,
    Role,
    SkillUse,
    SystemSetting,
)

log = logging.getLogger("agent_hq.learning")

DEFAULTS: dict[str, Any] = {
    "reflect": "signals",        # off | signals | every_job
    "provider_id": None,         # reflection model; none = each agent's own model
    "model": "",
    "on_feedback": True,         # 👎, or 👍 on multi-step work
    "on_correction": True,       # you push back in the conversation
    "on_failure": True,          # failed or partial jobs
    "min_tool_calls": 4,         # work with at least this many tool calls (0 = never)
    "wait_minutes": 10,          # quiet time after a job before reflecting on it
    "feedback_wait_minutes": 2,  # after a rating (time to add a note)
    "max_per_day": 30,
    "lessons_to_memory": True,
}

MAX_PER_JOB = 2   # a second look is allowed when you rate or correct after the first

# Phrases that usually mean "that's not what I wanted". A match only earns the job a look;
# the reflection itself decides whether there's anything to learn.
_CORRECTION_START = re.compile(
    r"^\W*(no\b|nope\b|wrong\b|not quite|not what|that'?s (not|wrong|incorrect)|"
    r"actually\b|instead\b|don'?t\b|do not\b|stop\b|why did you|i (said|asked|told you|meant)\b|"
    r"you (should|shouldn'?t|forgot|missed|didn'?t|need to)\b|next time\b|in future\b|"
    r"from now on\b|please don'?t\b)", re.I)
_CORRECTION_ANYWHERE = re.compile(
    r"(that'?s (wrong|not right|incorrect|not what i)|not what i (asked|wanted|meant)|"
    r"you should have|you shouldn'?t have|next time,? |from now on|in (the )?future,? |"
    r"i (already )?told you|wrong (one|server|host|file|place|answer))", re.I)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- settings


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "learning")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    if merged["reflect"] not in ("off", "signals", "every_job"):
        raise ValueError("reflect is off, signals or every_job")
    for k in ("min_tool_calls", "wait_minutes", "feedback_wait_minutes", "max_per_day"):
        merged[k] = max(0, int(merged[k] or 0))
    with db.session() as s:
        row = s.get(SystemSetting, "learning") or SystemSetting(key="learning")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


# --------------------------------------------------------------------------- signals


def looks_like_correction(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(_CORRECTION_START.search(t) or _CORRECTION_ANYWHERE.search(t))


def _learns(agent: AgentDef | None) -> bool:
    return agent is not None and skills.mode_of(agent) != LearningMode.off


def _agent(agent_id: str | None) -> AgentDef | None:
    if not agent_id:
        return None
    with db.session() as s:
        return s.get(AgentDef, agent_id)


def _kind(reason: str) -> str:
    return reason.split(":", 1)[0].strip()


def schedule(job_id: str, reasons: list[str], delay_minutes: float, *,
             strong: bool = False) -> Reflection | None:
    """Give a job a look in `delay_minutes`. A waiting reflection for the job just gets the
    new reasons (and the new time). `strong` signals (your rating or correction) may add a
    second look after the first one ran."""
    cfg = get_settings()
    if cfg["reflect"] == "off" or not reasons:
        return None
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None
        agent = s.get(AgentDef, job.assignee_agent_id)
        if not _learns(agent):
            return None
        rows = list(s.exec(select(Reflection).where(Reflection.job_id == job_id)).all())
        due = _now() + timedelta(minutes=max(0.0, delay_minutes))
        waiting = next((r for r in rows if r.status == "waiting"), None)
        if waiting is not None:
            # A newer rating replaces the earlier one ("you rated it 👎" then the note).
            kinds = {_kind(x) for x in reasons}
            kept = [x for x in waiting.reasons if _kind(x) not in kinds or not _kind(x).startswith("you rated")]
            waiting.reasons = list(dict.fromkeys([*kept, *reasons]))
            waiting.due_at = due
            s.add(waiting)
            s.commit()
            s.refresh(waiting)
            return waiting
        if rows and (not strong or len(rows) >= MAX_PER_JOB
                     or any(r.status == "running" for r in rows)):
            return None
        r = Reflection(job_id=job_id, thread_id=job.origin_thread_id, agent_id=agent.id,
                       agent_name=agent.name, reasons=list(dict.fromkeys(reasons)), due_at=due)
        s.add(r)
        s.commit()
        s.refresh(r)
        return r


def job_finished(job_id: str, outcome: str) -> None:
    """Called when a turn or task ends: does it deserve a look?"""
    try:
        cfg = get_settings()
        if cfg["reflect"] == "off":
            return
        with db.session() as s:
            job = s.get(Job, job_id)
        if job is None or (job.meta or {}).get("kind") == "reflection":
            return
        calls = int((job.meta or {}).get("tool_calls") or 0)
        reasons = []
        if cfg["on_failure"] and outcome in ("failed", "partial"):
            reasons.append(f"it {outcome if outcome == 'failed' else 'only partly worked'}")
        if cfg["min_tool_calls"] and calls >= int(cfg["min_tool_calls"]):
            reasons.append(f"{calls} tool calls")
        if cfg["reflect"] == "every_job" and not reasons:
            reasons.append("every job gets a look")
        if reasons:
            schedule(job_id, reasons, float(cfg["wait_minutes"]))
    except Exception as e:  # learning must never break a run
        log.warning("could not consider job %s for reflection: %s", job_id, e)


def last_reply(thread_id: str) -> Message | None:
    """The newest agent reply in a conversation that came from a job."""
    with db.session() as s:
        rows = s.exec(select(Message).where(Message.thread_id == thread_id,
                                            Message.role == Role.agent)
                      .order_by(Message.created_at.desc()).limit(5)).all()
    return next((m for m in rows if (m.meta or {}).get("job_id")), None)


def user_said(thread_id: str, text: str) -> None:
    """A new message from you: if it corrects the last reply, that job gets a look."""
    try:
        cfg = get_settings()
        if cfg["reflect"] == "off" or not cfg["on_correction"] or not looks_like_correction(text):
            return
        m = last_reply(thread_id)
        if m is None:
            return
        quote = text.strip().replace("\n", " ")
        quote = quote if len(quote) <= 160 else quote[:160] + "…"
        schedule(m.meta["job_id"], [f"you corrected it: “{quote}”"], float(cfg["wait_minutes"]),
                 strong=True)
    except Exception as e:
        log.warning("could not check a message for corrections: %s", e)


def tool_calls(job_id: str | None) -> int:
    if not job_id:
        return 0
    with db.session() as s:
        job = s.get(Job, job_id)
    return int(((job.meta or {}) if job else {}).get("tool_calls") or 0)


def used_skills(job_id: str | None) -> bool:
    if not job_id:
        return False
    with db.session() as s:
        return s.exec(select(func.count()).select_from(SkillUse)
                      .where(SkillUse.job_id == job_id)).one() > 0


def set_feedback(message: Message, rating: str | None, comment: str = "") -> Feedback | None:
    """Record (or clear, with rating None) your rating of an agent reply."""
    job_id = (message.meta or {}).get("job_id")
    comment = (comment or "").strip()[:2000]
    with db.session() as s:
        fb = s.exec(select(Feedback).where(Feedback.message_id == message.id)).first()
        if rating is None:
            if fb is not None:
                s.delete(fb)
        else:
            fb = fb or Feedback(message_id=message.id, thread_id=message.thread_id, job_id=job_id,
                                agent_id=message.agent_id)
            fb.rating, fb.comment, fb.updated_at = rating, comment, _now()
            s.add(fb)
        if job_id:
            for u in s.exec(select(SkillUse).where(SkillUse.job_id == job_id)).all():
                u.rating = rating
                s.add(u)
        s.commit()
        if fb is not None and rating is not None:
            s.refresh(fb)
    if rating is None or not job_id:
        return fb if rating else None
    try:
        cfg = get_settings()
        if cfg["on_feedback"]:
            if rating == "down":
                why = "you rated it 👎" + (f": “{comment[:200]}”" if comment else "")
                schedule(job_id, [why], float(cfg["feedback_wait_minutes"]), strong=True)
            elif tool_calls(job_id) >= 2 or used_skills(job_id):
                why = "you rated it 👍" + (f": “{comment[:200]}”" if comment else "")
                schedule(job_id, [why], float(cfg["feedback_wait_minutes"]), strong=True)
    except Exception as e:
        log.warning("could not schedule a reflection for feedback: %s", e)
    return fb


# --------------------------------------------------------------------------- reflecting


class SkillIdea(BaseModel):
    name: str = Field(description="lowercase_with_underscores, e.g. check_pve_backups; for a "
                      "revision, the existing skill's name")
    title: str = Field(description="a short title")
    when_to_use: str = Field(description="one or two sentences on the situations it's for")
    steps: str = Field(description="the full procedure in markdown: steps, checks, pitfalls")
    reason: str = Field(description="what happened that makes this worth keeping")
    revise: str | None = Field(default=None, description="name of an existing skill this "
                               "replaces with a corrected version; omit for a new skill")
    only_for_me: bool = Field(default=False, description="true if only you should use it")


class Lesson(BaseModel):
    text: str = Field(description="one self-contained sentence, e.g. \"Mike wants backup "
                      "reports as a table with failures first.\"")
    kind: Literal["preference", "fact", "procedure", "incident"] = "preference"
    only_for_me: bool = Field(default=False, description="true if it only matters for your "
                              "own work; false if the whole team should know it")


class ReflectionOut(BaseModel):
    summary: str = Field(description="one or two sentences: what happened and what you took from it")
    skills: list[SkillIdea] = Field(default_factory=list, description="usually none or one")
    lessons: list[Lesson] = Field(default_factory=list, description="usually none to two")


REFLECT_RULES = """\
You are {name}, looking back at work you just finished, to learn from it. You are not
talking to anyone now: nothing you write here is shown as a reply.

Decide what, if anything, is worth keeping for next time:
- skills: a procedure you or a teammate could follow next time a task like this comes up.
  Worth it when the work took several steps that worked (or failed, and you can now see
  the fix). If a skill you used was wrong or incomplete, put its name in `revise` and
  write the full corrected procedure. Don't create a skill that duplicates one listed
  below: revise that one instead.
- lessons: short statements for your memory: a preference or correction from the user
  (what they want, how they want it), a fact about the environment you discovered, a
  pitfall to avoid. One self-contained sentence each.

Rules:
- What the user said matters most. If they corrected you or rated a reply down, work out
  what they actually wanted and capture that.
- Only what the record below shows. Don't invent steps, hosts, tools or results.
- Don't keep one-off details of this task (today's numbers, this conversation's topic).
- Empty lists are a normal, good outcome when there's nothing worth keeping.
- Write `summary` in the first person, one or two sentences."""


def _fmt_time(dt: datetime | None) -> str:
    return _aware(dt).strftime("%Y-%m-%d %H:%M") if dt else "?"


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …"


def context_for(r: Reflection, agent: AgentDef) -> str:
    """Everything the agent sees when it looks back at a job."""
    with db.session() as s:
        job = s.get(Job, r.job_id)
        if job is None:
            raise LookupError("the job is gone")
        names = {a.id: a.name for a in s.exec(select(AgentDef)).all()}
        start = _aware(job.created_at) - timedelta(seconds=1)
        before = list(s.exec(select(Message).where(Message.thread_id == job.origin_thread_id,
                                                   Message.created_at < start)
                             .order_by(Message.created_at.desc()).limit(4)).all())[::-1]
        after = list(s.exec(select(Message).where(Message.thread_id == job.origin_thread_id,
                                                  Message.created_at >= start)
                            .order_by(Message.created_at).limit(30)).all())
        feedback = list(s.exec(select(Feedback).where(Feedback.job_id == job.id)).all())
        uses = list(s.exec(select(SkillUse).where(SkillUse.job_id == job.id)).all())

    def line(m: Message) -> str:
        who = ("You (the user)" if m.role == Role.user else
               names.get(m.agent_id or "", "agent") if m.role == Role.agent else "event")
        mark = ""
        if (m.meta or {}).get("job_id") == job.id:
            mark = "  ← the reply from this job"
        fb = (m.meta or {}).get("feedback")
        if fb:
            mark += f"  [user rated this {'👍' if fb.get('rating') == 'up' else '👎'}" + \
                    (f": {fb['comment']}" if fb.get("comment") else "") + "]"
        return f"[{_fmt_time(m.created_at)}] {who}: {_clip(m.content, 1500)}{mark}"

    parts = [f"Why this is worth a look: {'; '.join(r.reasons)}."]
    if job.kind.value == "task":
        requester = names.get(job.requester_agent_id or "", "the user")
        parts.append(f"The task {requester} delegated to you:\n{_clip(job.task, 3000)}")
        res = job.result or {}
        parts.append(f"What you handed back: [{res.get('status', job.status.value)}] "
                     f"{_clip(str(res.get('summary', '')), 1500)}\n{_clip(str(res.get('details', '')), 1500)}")
    else:
        parts.append(f"The request you were working on:\n{_clip(job.task, 3000)}")
        parts.append(f"How it ended: {job.status.value}" + (f" ({_clip(job.error, 500)})" if job.error else ""))
    trace = (job.meta or {}).get("trace") or []
    if trace:
        steps = []
        for i, t in enumerate(trace, 1):
            out = f"error: {t['error']}" if t.get("error") else t.get("result", "")
            steps.append(f"{i}. {t['tool']}({t.get('args', '')}) → {_clip(out, 300)}")
        parts.append("The tool calls you made, in order:\n" + "\n".join(steps))
    else:
        parts.append("You made no tool calls.")
    if uses:
        lines = []
        for u in uses:
            k = None
            with db.session() as s:
                from .models import Skill
                k = s.get(Skill, u.skill_id)
            if k is not None:
                lines.append(f"### {k.name} (version {u.version}; current version {k.version})\n"
                             f"When to use: {k.when_to_use}\n{_clip(k.body, 3000)}")
        if lines:
            parts.append("Skills you loaded for this job:\n" + "\n\n".join(lines))
    if feedback:
        parts.append("The user's ratings: " + "; ".join(
            f"{'👍' if f.rating == 'up' else '👎'}" + (f" “{f.comment}”" if f.comment else "")
            for f in feedback))
    convo = [line(m) for m in before] + (["--- this job starts ---"] if before else []) + \
            [line(m) for m in after]
    if convo:
        parts.append("The conversation (oldest first):\n" + "\n".join(convo))
    existing = skills.visible_to(agent)
    if existing:
        parts.append("Skills that already exist:\n" + "\n".join(
            f"- {k.name}: {k.title} (use when: {_clip(k.when_to_use, 160)})" for k in existing[:40]))
    return "\n\n".join(parts)


def _model_for(agent: AgentDef, cfg: dict[str, Any]):
    from .llm import build_agent_model, build_model_for, describe_model

    if cfg.get("model"):
        return (build_model_for(cfg.get("provider_id"), cfg["model"], "the reflection model"),
                cfg.get("provider_id"), cfg["model"], describe_model(cfg.get("provider_id"), cfg["model"]))
    return (build_agent_model(agent), agent.provider_id, agent.model,
            describe_model(agent.provider_id, agent.model))


def _finish(r_id: str, **fields: Any) -> Reflection:
    with db.session() as s:
        r = s.get(Reflection, r_id)
        for k, v in fields.items():
            setattr(r, k, v)
        r.finished_at = _now()
        s.add(r)
        s.commit()
        s.refresh(r)
        return r


def _claim(r_id: str) -> bool:
    """Take a waiting reflection; only one process gets it."""
    with db.session() as s:
        res = s.exec(update(Reflection).where(Reflection.id == r_id, Reflection.status == "waiting")
                     .values(status="running", started_at=_now()))
        s.commit()
        return (res.rowcount or 0) == 1


def ran_today() -> int:
    since = _now() - timedelta(hours=24)
    with db.session() as s:
        return s.exec(select(func.count()).select_from(Reflection).where(
            Reflection.started_at >= since, Reflection.status.in_(("done", "failed", "running")))).one()


async def reflect(rt, r_id: str) -> Reflection:
    """Run one reflection (already claimed) and act on what it finds."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from .runtime import load_agent, memory_on

    with db.session() as s:
        r = s.get(Reflection, r_id)
    try:
        agent = load_agent(r.agent_id)
    except Exception:
        return _finish(r_id, status="skipped", summary="The agent no longer exists.")
    if not _learns(agent):
        return _finish(r_id, status="skipped", summary=f"{agent.name}'s learning is off.")
    cfg = get_settings()
    started, usage, ok, model_name = time.monotonic(), None, False, ""
    provider_id = None
    try:
        context = context_for(r, agent)
        model, provider_id, model_id, model_name = _model_for(agent, cfg)
        reviewer = Agent(model, output_type=ReflectionOut, retries=2,
                         instructions=REFLECT_RULES.format(name=agent.name),
                         name=f"{agent.name} (reflection)")
        result = await reviewer.run(context, usage_limits=UsageLimits(request_limit=6))
        usage, ok, out = result.usage, True, result.output
    except Exception as e:
        log.warning("reflection %s failed: %s", r_id, e)
        return _finish(r_id, status="failed", error=f"{type(e).__name__}: {e}"[:2000], model=model_name)
    finally:
        if provider_id is not None or model_name:
            subagents.record_usage(agent, provider_id=provider_id, model=model_name.split(" @ ")[0],
                                   usage=usage, seconds=time.monotonic() - started, ok=ok,
                                   job_id=None, thread_id=r.thread_id)

    notes, proposal_ids, memory_ids = [], [], []
    for idea in out.skills[:2]:
        got = await rt.submit_skill_proposal(
            agent, name=idea.name.strip().lower(), title=idea.title, when_to_use=idea.when_to_use,
            steps=idea.steps, reason=idea.reason, revise=idea.revise or None,
            only_for_me=idea.only_for_me, thread_id=r.thread_id, job_id=r.job_id,
            source="reflection")
        if got.get("proposal_id"):
            proposal_ids.append(got["proposal_id"])
        else:
            notes.append(f"skill {idea.name} not proposed: {got.get('summary', '')}")
    lessons = [x for x in out.lessons[:3] if x.text.strip()]
    if lessons and cfg["lessons_to_memory"] and memory_on(agent):
        saved = []
        for x in lessons:
            try:
                m = await memory.capture(
                    x.text.strip(), kind=MemoryKind(x.kind),
                    scope=MemoryScope.agent if x.only_for_me else MemoryScope.team,
                    agent_id=agent.id, thread_id=r.thread_id, source="reflection",
                    source_job_id=r.job_id, created_by=agent.id)
                memory_ids.append(m.id)
                saved.append(x.text.strip())
            except Exception as e:
                notes.append(f"lesson not saved: {e}")
        if saved:
            await rt.save_message(
                r.thread_id, Role.event,
                f"{agent.name} noted for next time:\n" + "\n".join(f"- {t}" for t in saved),
                agent_id=agent.id, kind="lesson", reflection_id=r.id, memory_ids=memory_ids)
    elif lessons:
        notes.append("lessons not saved (memory is off for this agent or for reflections)")
    summary = out.summary.strip() + ("\n" + "\n".join(notes) if notes else "")
    return _finish(r_id, status="done", summary=summary[:4000], proposal_ids=proposal_ids,
                   memory_ids=memory_ids, model=model_name)


async def run_due(rt, limit: int = 3) -> list[Reflection]:
    """Run reflections whose time has come (up to `limit`), within the daily cap."""
    cfg = get_settings()
    with db.session() as s:
        due = list(s.exec(select(Reflection).where(Reflection.status == "waiting",
                                                   Reflection.due_at <= _now())
                          .order_by(Reflection.due_at).limit(limit)).all())
    done = []
    for r in due:
        if cfg["reflect"] == "off":
            if _claim(r.id):
                done.append(_finish(r.id, status="skipped", summary="Reflection is turned off."))
            continue
        if cfg["max_per_day"] and ran_today() >= int(cfg["max_per_day"]):
            if _claim(r.id):
                done.append(_finish(r.id, status="skipped",
                                    summary=f"Skipped: the daily limit ({cfg['max_per_day']}) was reached."))
            continue
        if _claim(r.id):
            done.append(await reflect(rt, r.id))
    return done


async def run_now(rt, r_id: str) -> Reflection:
    """Run one reflection now, whatever its due time or the daily cap ("Run now")."""
    with db.session() as s:
        r = s.get(Reflection, r_id)
        if r is None:
            raise LookupError(r_id)
        if r.status in ("skipped", "failed"):  # run it again
            r.status, r.error = "waiting", None
            s.add(r)
            s.commit()
    if not _claim(r_id):
        raise ValueError("this reflection is already running or done")
    return await reflect(rt, r_id)


def recover_stuck(minutes: int = 30) -> None:
    """Reflections left 'running' by a restart go back to waiting."""
    cutoff = _now() - timedelta(minutes=minutes)
    with db.session() as s:
        s.exec(update(Reflection).where(Reflection.status == "running", Reflection.started_at < cutoff)
               .values(status="waiting"))
        s.commit()


async def loop(rt, every: float = 30) -> None:
    """Background: run due reflections (one process at a time, via a Valkey lock)."""
    while True:
        try:
            recover_stuck()
            if await rt.broker.r.set("ahq:learning:runner", "1", nx=True, ex=600):
                try:
                    await run_due(rt)
                finally:
                    await rt.broker.r.delete("ahq:learning:runner")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("learning loop failed")
        await asyncio.sleep(every)
