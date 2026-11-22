"""Skill upkeep: usage, staleness, the scheduled skill review, archiving.

* Every load of a skill is recorded with how the job turned out and your 👍 / 👎 on the
  reply (app/skills.py), so each skill shows its uses, when it was last used and its ratings.
* A skill is **stale** when nobody has used it for N days (or, never used, it was added more
  than N days ago), or when its ratings are poor: at least `min_ratings` 👍/👎 and a share of
  👎 at or above `poor_share`. Stale skills are flagged on the Skills page; nothing happens to
  them by itself.
* The optional **skill review** runs on a schedule (or when you press Run now) on the model
  you choose. It looks over every active skill, how much it's used and how it's rated, and
  proposes merges, rewrites and archiving. Each idea is a proposal in Requests → Skill
  proposals, tested by the evals like any other change, and nothing changes until you
  approve it.
* **Archived** skills are hidden from agents, with their history and usage kept; restore one
  any time from the Skills page.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlmodel import select

from . import db, skills, subagents
from .config import settings as app_settings
from .models import Skill, SkillProposal, SkillReview, SystemSetting

log = logging.getLogger("agent_hq.skill_upkeep")

DEFAULTS: dict[str, Any] = {
    "stale_after_days": 60,     # unused this long = stale (0 = never by age)
    "min_ratings": 3,           # ratings needed before they can make a skill stale
    "poor_share": 0.5,          # share of 👎 at or above this = poor
    "review": "off",            # off | daily | weekly | monthly
    "review_hour": 3,           # local time (AHQ_TIMEZONE)
    "review_weekday": 0,        # weekly: 0 = Sunday … 6 = Saturday
    "review_day": 1,            # monthly: day of the month (1-28)
    "provider_id": None,        # the review model; none = the evals judge, else the first agent's
    "model": "",
    "max_suggestions": 5,       # proposals per review
}
REVIEWS = ("off", "daily", "weekly", "monthly")
STATE_KEY = "skill_upkeep_state"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- settings


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "skill_upkeep")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    if merged["review"] not in REVIEWS:
        raise ValueError("review is off, daily, weekly or monthly")
    merged["stale_after_days"] = max(0, int(merged["stale_after_days"] or 0))
    merged["min_ratings"] = max(1, int(merged["min_ratings"] or 1))
    merged["poor_share"] = min(1.0, max(0.05, float(merged["poor_share"] or 0.5)))
    merged["review_hour"] = min(23, max(0, int(merged["review_hour"] or 0)))
    merged["review_weekday"] = min(6, max(0, int(merged["review_weekday"] or 0)))
    merged["review_day"] = min(28, max(1, int(merged["review_day"] or 1)))
    merged["max_suggestions"] = min(20, max(1, int(merged["max_suggestions"] or 1)))
    merged["model"] = (merged.get("model") or "").strip()
    with db.session() as s:
        row = s.get(SystemSetting, "skill_upkeep") or SystemSetting(key="skill_upkeep")
        row.value = merged
        s.add(row)
        s.commit()
    _set_state(next_review_at=_iso(next_review(merged)))
    return merged


def _state() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, STATE_KEY)
    return dict(row.value) if row else {}


def _set_state(**values: Any) -> None:
    with db.session() as s:
        row = s.get(SystemSetting, STATE_KEY) or SystemSetting(key=STATE_KEY)
        row.value = {**(row.value or {}), **values}
        s.add(row)
        s.commit()


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def cron_for(cfg: dict[str, Any]) -> str | None:
    from .scheduler import build_cron

    if cfg["review"] == "off":
        return None
    if cfg["review"] == "weekly":
        return build_cron("weekly", hour=cfg["review_hour"], weekdays=[cfg["review_weekday"]])
    if cfg["review"] == "monthly":
        return build_cron("monthly", hour=cfg["review_hour"], day_of_month=cfg["review_day"])
    return build_cron("daily", hour=cfg["review_hour"])


def next_review(cfg: dict[str, Any] | None = None, after: datetime | None = None) -> datetime | None:
    from .scheduler import next_runs

    cfg = cfg or get_settings()
    cron = cron_for(cfg)
    if not cron:
        return None
    tz = (app_settings.timezone or "UTC").strip() or "UTC"
    try:
        return next_runs(cron, tz, after=after)[0]
    except Exception:
        return next_runs(cron, "UTC", after=after)[0]


def status() -> dict[str, Any]:
    st, cfg = _state(), get_settings()
    nxt = st.get("next_review_at") if cfg["review"] != "off" else None
    if cfg["review"] != "off" and not nxt:
        nxt = _iso(next_review(cfg))
    return {"last_review_at": st.get("last_review_at"), "next_review_at": nxt,
            "timezone": app_settings.timezone or "UTC"}


# --------------------------------------------------------------------------- staleness


def flags(k: Skill, st: dict[str, Any] | None, cfg: dict[str, Any] | None = None,
          now: datetime | None = None) -> list[str]:
    """Why a skill is stale ([] = it isn't)."""
    cfg, now = cfg or get_settings(), now or _now()
    g = st or {}
    out = []
    days = int(cfg["stale_after_days"])
    if days:
        last = _aware(g.get("last_used_at"))
        if last is not None:
            idle = (now - last).days
            if idle >= days:
                out.append(f"unused for {idle} days")
        else:
            age = (now - _aware(k.created_at)).days
            if age >= days:
                out.append(f"never used (added {age} days ago)")
    up, down = int(g.get("up", 0)), int(g.get("down", 0))
    if up + down >= int(cfg["min_ratings"]) and down / (up + down) >= float(cfg["poor_share"]):
        out.append(f"poorly rated (👎 {down} of {up + down})")
    return out


# --------------------------------------------------------------------------- the review


class NewText(BaseModel):
    title: str = Field(description="a short title")
    when_to_use: str = Field(description="one or two sentences on the situations it's for")
    steps: str = Field(description="the full procedure in markdown: steps, checks, pitfalls")


class Suggestion(BaseModel):
    action: Literal["merge", "rewrite", "archive"]
    skills: list[str] = Field(description="the skill names it's about. merge: the one to keep first, "
                              "then the ones folded into it; rewrite and archive: one name")
    reason: str = Field(description="why, citing the usage, ratings or overlap you saw")
    new: NewText | None = Field(default=None, description="merge and rewrite: the complete new text "
                                "of the kept skill; leave out for archive")


class ReviewOut(BaseModel):
    summary: str = Field(description="two or three sentences on the state of the skills")
    suggestions: list[Suggestion] = Field(default_factory=list)


REVIEW_RULES = """\
You look after a team of AI agents' skills: written-down procedures the agents load and
follow. Review them and suggest the few changes that would help most. The user approves
each one, and each is tested against the team's test cases first.

- merge: two or more skills cover the same job. Keep the best-named one (list it first) and
  write the complete merged procedure; the others are archived.
- rewrite: a skill that's poorly rated, fails often, is unclear, or contradicts another.
  Write the complete corrected procedure. Keep what works; don't invent hosts or commands.
- archive: a skill nobody uses any more, or that's wrong and not worth fixing.
- Suggest at most {limit} changes, best first, and none if the skills are in good shape.
  A skill marked stale isn't automatically worth archiving: a rarely needed procedure
  (disaster recovery, a yearly renewal) may be worth keeping."""


def _model(cfg: dict[str, Any]):
    from . import evals
    from .llm import build_agent_model, build_model_for, describe_model
    from .models import AgentDef

    if cfg.get("model"):
        return (build_model_for(cfg.get("provider_id"), cfg["model"], "the skill review model"),
                cfg.get("provider_id"), cfg["model"], describe_model(cfg.get("provider_id"), cfg["model"]))
    ev = evals.get_settings()
    if ev.get("judge_model"):
        return (build_model_for(ev.get("judge_provider_id"), ev["judge_model"], "the judge model"),
                ev.get("judge_provider_id"), ev["judge_model"],
                describe_model(ev.get("judge_provider_id"), ev["judge_model"]))
    with db.session() as s:
        a = s.exec(select(AgentDef).where(AgentDef.enabled == True, AgentDef.is_subagent == False)  # noqa: E712
                   .order_by(AgentDef.created_at)).first()
    if a is None:
        raise ValueError("choose a model for the skill review (Settings → Skills → Upkeep)")
    return build_agent_model(a), a.provider_id, a.model, describe_model(a.provider_id, a.model)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


def context(rows: list[Skill], st: dict[str, dict], cfg: dict[str, Any]) -> str:
    lines = []
    for k in rows:
        g = st.get(k.id) or {}
        finished = g.get("done", 0) + g.get("partial", 0) + g.get("failed", 0)
        last = _aware(g.get("last_used_at"))
        stale = flags(k, g, cfg)
        lines.append("\n".join([
            f"## {k.name} (v{k.version}{', only for one agent' if k.scope == 'agent' else ''})",
            f"Title: {k.title}",
            f"Use when: {_clip(k.when_to_use, 400)}",
            f"Usage: {g.get('uses', 0)} uses"
            + (f", {g.get('done', 0)}/{finished} jobs done" if finished else "")
            + f", 👍 {g.get('up', 0)} 👎 {g.get('down', 0)}"
            + (f", last used {last:%Y-%m-%d}" if last else ", never used")
            + f", added {_aware(k.created_at):%Y-%m-%d}"
            + (f". STALE: {'; '.join(stale)}" if stale else ""),
            "Procedure:", _clip(k.body, 2500)]))
    return f"Today is {_now():%Y-%m-%d}. The team's {len(rows)} active skills:\n\n" + "\n\n".join(lines)


def _busy_names() -> set[str]:
    """Skills that already have a proposal waiting (the review leaves those alone)."""
    with db.session() as s:
        return {p.name for p in s.exec(select(SkillProposal).where(
            SkillProposal.status.in_(("pending", "testing")))).all()}


async def review(rt, *, trigger: str = "manual", review_id: str | None = None) -> SkillReview:
    """Run the skill review once and turn its suggestions into proposals."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    from . import evals

    cfg = get_settings()
    if review_id:
        with db.session() as s:
            r = s.get(SkillReview, review_id)
    else:
        r = SkillReview(trigger=trigger)
        with db.session() as s:
            s.add(r)
            s.commit()
            s.refresh(r)
    _set_state(last_review_at=_iso(_now()))

    def finish(**fields: Any) -> SkillReview:
        with db.session() as s:
            row = s.get(SkillReview, r.id)
            for k, v in fields.items():
                setattr(row, k, v)
            row.finished_at = _now()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row

    with db.session() as s:
        rows = list(s.exec(select(Skill).where(Skill.status == "active").order_by(Skill.name)).all())
    if not rows:
        return finish(status="done", summary="There are no active skills to review.")
    st = skills.stats([k.id for k in rows])
    model_name, provider_id, usage, ok, started = "", None, None, False, time.monotonic()
    try:
        model, provider_id, model_id, model_name = _model(cfg)
        reviewer = Agent(model, output_type=ReviewOut, retries=2, name="skill review",
                         instructions=REVIEW_RULES.format(limit=cfg["max_suggestions"]))
        result = await reviewer.run(context(rows, st, cfg), usage_limits=UsageLimits(request_limit=6))
        usage, ok, out = result.usage, True, result.output
    except Exception as e:
        log.warning("skill review %s failed: %s", r.id, e)
        return finish(status="failed", error=f"{type(e).__name__}: {e}"[:2000], model=model_name)
    finally:
        if model_name:
            try:
                who = SimpleNamespace(id="", name="Skill review", is_subagent=False, parent_agent_id=None)
                subagents.record_usage(who, provider_id=provider_id, model=model_name.split(" @ ")[0],
                                       usage=usage, seconds=time.monotonic() - started, ok=ok,
                                       job_id=None, thread_id=None)
            except Exception:
                pass

    by_name = {k.name: k for k in rows}
    busy, notes, ids = _busy_names(), [], []
    for idea in out.suggestions[: int(cfg["max_suggestions"])]:
        names = [n.strip().lower() for n in idea.skills if n.strip()]
        unknown = [n for n in names if n not in by_name]
        if not names or unknown:
            notes.append(f"{idea.action} skipped: no skill named {', '.join(unknown) or '(none given)'}")
            continue
        if busy & set(names):
            notes.append(f"{idea.action} {names[0]} skipped: a proposal for it is already waiting")
            continue
        keep = by_name[names[0]]
        if idea.action == "archive":
            for n in names:
                p = _proposal(by_name[n], action="archive", reason=idea.reason)
                ids.append(await _submit(p, evals))
                busy.add(n)
            continue
        if idea.new is None:
            notes.append(f"{idea.action} {keep.name} skipped: no new text given")
            continue
        why = skills.check_fields(keep.name, idea.new.title, idea.new.when_to_use, idea.new.steps)
        if why:
            notes.append(f"{idea.action} {keep.name} skipped: {why}")
            continue
        merge = [by_name[n].id for n in names[1:]] if idea.action == "merge" else []
        if idea.action == "merge" and not merge:
            notes.append(f"merge {keep.name} skipped: name the skills to fold into it")
            continue
        p = _proposal(keep, action="merge" if merge else "save", reason=idea.reason, new=idea.new, merge_ids=merge)
        ids.append(await _submit(p, evals))
        busy |= set(names)
    summary = out.summary.strip() + ("\n" + "\n".join(notes) if notes else "")
    return finish(status="done", summary=summary[:4000], proposal_ids=ids, model=model_name)


def _proposal(k: Skill, *, action: str, reason: str, new: NewText | None = None,
              merge_ids: list[str] | None = None) -> SkillProposal:
    return SkillProposal(skill_id=k.id, name=k.name, title=new.title if new else k.title,
                         when_to_use=new.when_to_use if new else k.when_to_use,
                         body=new.steps if new else k.body, tools=list(k.tools or []), script=k.script,
                         scope=k.scope, reason=reason.strip()[:2000], source="curator", action=action,
                         merge_ids=list(merge_ids or []), agent_id=k.agent_id or "",
                         agent_name="Skill review", base_version=k.version)


async def _submit(p: SkillProposal, evals) -> str:
    with db.session() as s:
        s.add(p)
        s.commit()
        s.refresh(p)
    cases = evals.cases_for_change(p) if evals.get_settings()["on_proposals"] else []
    if cases:
        evals.queue_proposal_run(p, cases)
    what = {"archive": "archiving", "merge": "merging into"}.get(p.action, "rewriting")
    await subagents.notify({"type": "skill_proposal", "proposal_id": p.id, "agent": p.agent_name,
                            "skill": p.name, "title": f"{what} {p.name}", "revision": True,
                            "reason": p.reason, "link": subagents.link(f"/requests/skills?skill={p.id}")})
    return p.id


def start(rt, trigger: str = "manual") -> SkillReview:
    """Start a review in the background; returns its row at once."""
    r = SkillReview(trigger=trigger)
    with db.session() as s:
        s.add(r)
        s.commit()
        s.refresh(r)

    async def run():
        try:
            await review(rt, trigger=trigger, review_id=r.id)
        except Exception:
            log.exception("skill review failed")

    asyncio.get_running_loop().create_task(run())
    return r


def recover_stuck(minutes: int = 60) -> None:
    cutoff = _now() - timedelta(minutes=minutes)
    with db.session() as s:
        for r in s.exec(select(SkillReview).where(SkillReview.status == "running",
                                                  SkillReview.started_at < cutoff)).all():
            r.status, r.error, r.finished_at = "failed", "interrupted (Agent HQ restarted)", _now()
            s.add(r)
        s.commit()


async def run_if_due(rt, now: datetime | None = None) -> SkillReview | None:
    cfg = get_settings()
    if cfg["review"] == "off":
        return None
    now = now or _now()
    st = _state()
    due = st.get("next_review_at")
    if not due:
        _set_state(next_review_at=_iso(next_review(cfg, after=now)))
        return None
    if datetime.fromisoformat(due) > now:
        return None
    _set_state(next_review_at=_iso(next_review(cfg, after=now)))
    return await review(rt, trigger="schedule")


async def loop(rt, every: float = 60) -> None:
    """Background: the scheduled review (one process at a time, via a Valkey lock)."""
    while True:
        try:
            recover_stuck()
            if await rt.broker.r.set("ahq:skill-review:runner", "1", nx=True, ex=1800):
                try:
                    await run_if_due(rt)
                finally:
                    await rt.broker.r.delete("ahq:skill-review:runner")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("skill upkeep loop failed")
        await asyncio.sleep(every)
