"""Evals: test cases that show whether agents (and the skills they learn) get better.

A test case is a request for an agent, the checks its answer must pass and the tool
results recorded from the job it came from. Running a case replays those recordings:

* a tool call that matches the recording gets the recorded result, so a test never
  touches your lab and gives the same answer every time;
* a call with no recording runs for real only if the tool is set to Allow *and* is safe
  in tests (Settings → Tools; tools the server declares read-only start out safe);
  anything else gets "not available in a test run", and the result shows it;
* Agent HQ's own read-only tools (skills, knowledge search) run normally; anything that
  would change something (memories, delegation, proposals) is stubbed.

Checks: text the reply must or mustn't contain, tools it must or mustn't call, a skill it
must load, and rubrics graded by the judge model you pick (any provider: a strong local
model, ChatGPT, or another OpenAI-compatible service).

Where cases come from: a 👎 with a note, or a correction in the conversation, drafts one
(you click Keep); "Make this a test" on any reply; or you write one.

When they run: when an agent proposes a skill change, the cases for that agent and skill
run without and with the change, and the proposal shows both pass rates. In Auto learning
mode a change that lowers the pass rate isn't applied: it waits for you in Requests.
Or run them yourself from Settings → Evals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, Field
from sqlmodel import delete, select, update

from . import db, skills
from .models import (
    AgentDef,
    EvalCase,
    EvalResult,
    EvalRun,
    Job,
    JobKind,
    JobTrace,
    Message,
    Skill,
    SkillProposal,
    SkillUse,
    SkillVersion,
    SystemSetting,
)

log = logging.getLogger("agent_hq.evals")

DEFAULTS: dict[str, Any] = {
    "judge_provider_id": None,   # the judge: any provider and model you've set up
    "judge_model": "",           # blank = each agent's own model (lenient: it grades itself)
    "on_proposals": True,        # test skill changes against the relevant cases
    "gate_auto": True,           # Auto mode: don't apply a change that lowers the pass rate
    "draft_from_feedback": True,  # 👎 notes and corrections draft cases
    "live_read_only": True,      # unrecorded calls to safe-in-tests tools run for real
    "max_cases": 20,             # per proposal run
    "keep_traces_days": 14,      # full tool results kept for making cases
}

CHECK_TYPES = ("rubric", "contains", "not_contains", "tool_called", "tool_not_called", "skill_loaded")

# Agent HQ's own tools that only read: they run normally in tests.
SAFE_INTERNAL = {"load_skill", "list_skills", "search_knowledge", "read_document", "recall"}

NOT_AVAILABLE = ("Not available in a test run: there's no recorded result for this call, and the "
                 "tool can't run for real in tests. Carry on without it.")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "evals")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    merged["max_cases"] = max(1, min(200, int(merged["max_cases"])))
    merged["keep_traces_days"] = max(1, min(365, int(merged["keep_traces_days"])))
    with db.session() as s:
        row = s.get(SystemSetting, "evals") or SystemSetting(key="evals")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


def clean_checks(checks: list[dict]) -> list[dict]:
    out = []
    for c in checks or []:
        t, v = str(c.get("type", "")).strip(), str(c.get("value", "")).strip()
        if t in CHECK_TYPES and v:
            out.append({"type": t, "value": v})
    return out


# --------------------------------------------------------------------------- making cases


def _context_before(thread_id: str | None, before: datetime, prompt: str = "", limit: int = 6) -> str:
    """The conversation before a job, without the message that started it."""
    if not thread_id:
        return ""
    with db.session() as s:
        rows = list(s.exec(select(Message).where(Message.thread_id == thread_id,
                                                 Message.created_at <= before,
                                                 Message.role.in_(("user", "agent")))
                           .order_by(Message.created_at.desc()).limit(limit + 1)).all())[::-1]
    if rows and rows[-1].role.value == "user" and rows[-1].content.strip() == prompt.strip():
        rows = rows[:-1]
    rows = rows[-limit:]
    parts = []
    for m in rows:
        text = m.content if len(m.content) <= 800 else m.content[:800] + " …"
        parts.append(f"{'User' if m.role.value == 'user' else 'You'}: {text}")
    return "\n".join(parts)


def recording_for(job_id: str) -> tuple[list[dict], bool]:
    """The job's tool calls with full results, or the abbreviated ones if they've expired."""
    with db.session() as s:
        row = s.get(JobTrace, job_id)
        if row and row.steps:
            return [dict(x) for x in row.steps], False
        job = s.get(Job, job_id)
    brief = ((job.meta or {}) if job else {}).get("trace") or []
    return [{"tool": t["tool"], "args": t.get("args", ""), "result": t.get("result"),
             "error": t.get("error")} for t in brief], bool(brief)


def case_from_job(job_id: str, *, source: str, rubric: str = "", status: str = "draft",
                  message_id: str | None = None, name: str = "") -> EvalCase:
    """A test case from a finished job: its request, recording and skills. A later 👎 note
    or correction on the same job adds to its draft instead of starting another."""
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            raise LookupError("the job behind that reply is gone")
        # Feedback adds to a draft for the job, never to a test you've kept; "Make this a
        # test" opens the case the job already has.
        q = select(EvalCase).where(EvalCase.source_job_id == job_id)
        q = q.where(EvalCase.status != "archived") if source == "reply" else q.where(EvalCase.status == "draft")
        existing = s.exec(q).first()
        used = [u.skill_id for u in s.exec(select(SkillUse).where(SkillUse.job_id == job_id)).all()]
        names = [k.name for k in s.exec(select(Skill).where(Skill.id.in_(used))).all()] if used else []
    rubric = rubric.strip()
    if existing is not None:
        if rubric and not any(c["type"] == "rubric" and c["value"] == rubric for c in existing.checks):
            with db.session() as s:
                row = s.get(EvalCase, existing.id)
                row.checks = [*row.checks, {"type": "rubric", "value": rubric}]
                row.updated_at = _now()
                s.add(row)
                s.commit()
                s.refresh(row)
                return row
        return existing
    recording, partial = recording_for(job_id)
    checks = [{"type": "rubric", "value": rubric}] if rubric else []
    title = name or (job.task.strip().splitlines()[0][:80] if job.task.strip() else "Test case")
    case = EvalCase(name=title, agent_id=job.assignee_agent_id,
                    kind="task" if job.kind == JobKind.task else "turn", prompt=job.task,
                    context=_context_before(job.origin_thread_id, job.created_at, job.task) if job.kind != JobKind.task else "",
                    checks=checks, recording=recording, recording_partial=partial, skill_names=names,
                    status=status, source=source, source_job_id=job_id, source_message_id=message_id,
                    thread_id=job.origin_thread_id)
    with db.session() as s:
        s.add(case)
        s.commit()
        s.refresh(case)
    return case


def draft_from_feedback(message: Message, comment: str) -> EvalCase | None:
    job_id = (message.meta or {}).get("job_id")
    if not job_id or not comment.strip() or not get_settings()["draft_from_feedback"]:
        return None
    try:
        return case_from_job(job_id, source="feedback", message_id=message.id,
                             rubric=f"The user rated an earlier answer down: “{comment.strip()}”. "
                                    "The reply does what they asked for there.")
    except Exception as e:
        log.warning("could not draft a test case: %s", e)
        return None


def draft_from_correction(job_id: str, text: str) -> EvalCase | None:
    if not get_settings()["draft_from_feedback"]:
        return None
    try:
        return case_from_job(job_id, source="correction",
                             rubric=f"The user corrected an earlier answer: “{text.strip()[:500]}”. "
                                    "The reply already gets this right.")
    except Exception as e:
        log.warning("could not draft a test case: %s", e)
        return None


# --------------------------------------------------------------------------- replay


def _canon(args: Any) -> str:
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return str(args)


@dataclass
class Replay:
    """Answers a test run's tool calls from the recording."""

    recording: list[dict]
    live_allowed: bool = True
    calls: list[dict] = field(default_factory=list)
    _used: set[int] = field(default_factory=set)

    def _match(self, name: str, args: Any) -> tuple[int, dict] | None:
        want = _canon(args)
        for exact in (True, False):
            for i, r in enumerate(self.recording):
                if i in self._used or r.get("tool") != name:
                    continue
                if not exact or _canon(r.get("args")) == want:
                    return i, r
        return None

    async def call(self, name: str, args: Any, source: str, inner: Callable[[], Awaitable[Any]]) -> Any:
        if source == "internal":
            if name in SAFE_INTERNAL:
                self.calls.append({"tool": name, "args": args, "answered": "live"})
                return await inner()
            self.calls.append({"tool": name, "args": args, "answered": "stubbed"})
            return ("(test run: this tool doesn't do anything in a test. Assume it worked, "
                    "and carry on.)")
        hit = self._match(name, args)
        if hit is not None:
            i, r = hit
            self._used.add(i)
            exact = _canon(r.get("args")) == _canon(args)
            self.calls.append({"tool": name, "args": args, "answered": "recorded" if exact else "recorded (other arguments)"})
            if r.get("error"):
                raise RuntimeError(r["error"])
            return r.get("result")
        # Not recorded: the approval wrapper decides whether it may run for real.
        self.calls.append({"tool": name, "args": args, "answered": "pending"})
        return await inner()

    def _last_pending(self, name: str) -> dict | None:
        # The approval wrapper sees the server's own tool name; calls are listed with the
        # server prefix the agent saw ("lab_list_vms").
        return next((c for c in reversed(self.calls) if c["answered"] == "pending"
                     and (c["tool"] == name or c["tool"].endswith("_" + name))), None)

    def went_live(self, name: str) -> None:
        c = self._last_pending(name)
        if c:
            c["answered"] = "live (safe in tests)"

    def not_available(self, name: str, args: Any, mode: str, safe: bool) -> str:
        c = self._last_pending(name)
        if c:
            c["answered"] = ("not available (" + ("approval: " + mode if mode != "allow" else
                             "not marked safe in tests" if not safe else "live calls are off") + ")")
        return NOT_AVAILABLE

    def finish(self) -> None:
        for c in self.calls:  # a call that never reached a tool server (e.g. unknown tool)
            if c["answered"] == "pending":
                c["answered"] = "not available"


# --------------------------------------------------------------------------- running


class Verdict(BaseModel):
    passed: bool = Field(description="true if the reply meets the criterion")
    reason: str = Field(description="one sentence: why")


JUDGE_RULES = """\
You grade an AI agent's reply against one criterion. Be strict and literal: pass it only
if the reply clearly meets the criterion. The agent's tool calls are listed for context;
in a test run some tools answer from a recording or aren't available, which is not the
agent's fault. Judge the reply, not the formatting."""


def _judge_model(agent: AgentDef):
    from .llm import build_agent_model, build_model_for, describe_model

    cfg = get_settings()
    if cfg.get("judge_model"):
        return (build_model_for(cfg.get("judge_provider_id"), cfg["judge_model"], "the judge model"),
                describe_model(cfg.get("judge_provider_id"), cfg["judge_model"]))
    return build_agent_model(agent), describe_model(agent.provider_id, agent.model) + " (the agent itself)"


async def _grade(case: EvalCase, reply: str, calls: list[dict], agent: AgentDef) -> list[dict]:
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    out = []
    called = [c["tool"] for c in calls]
    loaded = [str((c.get("args") or {}).get("name", "")).lower() for c in calls
              if c["tool"] == "load_skill" and isinstance(c.get("args"), dict)]
    low = reply.lower()
    judge = None
    for chk in case.checks:
        t, v = chk["type"], chk["value"]
        if t == "contains":
            out.append({"check": chk, "passed": v.lower() in low, "note": ""})
        elif t == "not_contains":
            out.append({"check": chk, "passed": v.lower() not in low, "note": ""})
        elif t == "tool_called":
            ok = any(c == v or c.endswith("_" + v) or c.endswith("." + v) for c in called)
            out.append({"check": chk, "passed": ok, "note": "" if ok else f"called: {', '.join(called) or 'nothing'}"})
        elif t == "tool_not_called":
            ok = not any(c == v or c.endswith("_" + v) or c.endswith("." + v) for c in called)
            out.append({"check": chk, "passed": ok, "note": ""})
        elif t == "skill_loaded":
            ok = v.lower() in loaded
            out.append({"check": chk, "passed": ok, "note": "" if ok else f"loaded: {', '.join(loaded) or 'none'}"})
        elif t == "rubric":
            if judge is None:
                model, judge_name = _judge_model(agent)
                judge = Agent(model, output_type=Verdict, retries=2, instructions=JUDGE_RULES, name="judge")
            steps = "\n".join(f"- {c['tool']} ({c['answered']})" for c in calls) or "(none)"
            prompt = (f"The request:\n{case.prompt}\n\nThe agent's tool calls:\n{steps}\n\n"
                      f"The agent's reply:\n{reply}\n\nThe criterion:\n{v}")
            try:
                r = await judge.run(prompt, usage_limits=UsageLimits(request_limit=4))
                out.append({"check": chk, "passed": bool(r.output.passed), "note": r.output.reason})
            except Exception as e:
                out.append({"check": chk, "passed": False, "note": f"the judge failed: {e}"})
    return out


def _variant_overlay(variant: str, proposal: SkillProposal | None) -> dict[str, Skill | None] | None:
    """What the skills look like without or with the proposed change."""
    if proposal is None or variant == "current":
        return None
    with db.session() as s:
        base = s.get(Skill, proposal.skill_id) if proposal.skill_id else None
        if base is None:
            base = s.exec(select(Skill).where(Skill.name == proposal.name)).first()
        base_ver = None
        if base is not None and proposal.base_version:
            base_ver = s.exec(select(SkillVersion).where(SkillVersion.skill_id == base.id,
                                                         SkillVersion.version == proposal.base_version)).first()
    scope = "agent" if proposal.scope == "agent" else "team"
    if variant == "with":
        return {proposal.name: Skill(id=f"eval-{proposal.id}", name=proposal.name, title=proposal.title,
                                     when_to_use=proposal.when_to_use, body=proposal.body,
                                     tools=proposal.tools or [], scope=scope, agent_id=proposal.agent_id,
                                     version=(proposal.base_version or 0) + 1)}
    # without: the skill as it was when the change was proposed (or not there at all)
    if base_ver is not None:
        return {proposal.name: Skill(id=base.id, name=base.name, title=base_ver.title,
                                     when_to_use=base_ver.when_to_use, body=base_ver.body,
                                     tools=base_ver.tools or [], scope=base.scope, agent_id=base.agent_id,
                                     version=base_ver.version)}
    return {proposal.name: None}


async def run_case(rt, case: EvalCase, *, variant: str = "current",
                   proposal: SkillProposal | None = None, run_id: str = "") -> EvalResult:
    from .runtime import RunDeps, as_work_result, load_agent

    started = time.monotonic()
    result = EvalResult(run_id=run_id, case_id=case.id, variant=variant)
    try:
        agent = load_agent(case.agent_id)
    except Exception:
        result.error = "the agent no longer exists"
        return result
    replay = Replay(recording=list(case.recording or []), live_allowed=bool(get_settings()["live_read_only"]))
    deps = RunDeps(thread_id=f"eval-{run_id or case.id}", agent_id=agent.id, depth=0, replay=replay)
    prompt = case.prompt if not case.context else (
        f"(Earlier in this conversation:\n{case.context}\n)\n\n{case.prompt}")
    token = skills.overlay.set(_variant_overlay(variant, proposal))
    try:
        reply, _ = await rt.run_agent(agent, prompt, deps, as_worker=case.kind == "task")
        if case.kind == "task":
            w = as_work_result(reply)
            reply = f"[{w.status}] {w.summary}\n{w.details or ''}".strip()
        result.reply = str(reply)
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"[:2000]
    finally:
        skills.overlay.reset(token)
    replay.finish()
    result.calls = [{**c, "args": _brief_args(c.get("args"))} for c in replay.calls]
    from .llm import describe_model

    result.model = describe_model(agent.provider_id, agent.model)
    if result.error is None:
        result.checks = await _grade(case, result.reply, replay.calls, agent)
        result.passed = bool(result.checks) and all(c["passed"] for c in result.checks)
    result.seconds = round(time.monotonic() - started, 1)
    return result


def _brief_args(args: Any) -> Any:
    text = _canon(args)
    return args if len(text) <= 600 else text[:600] + "…"


def cases_for_proposal(agent: AgentDef, skill_name: str) -> list[EvalCase]:
    """Active cases that a change to `skill_name` by `agent` could affect."""
    owner = agent.parent_agent_id if agent.is_subagent and agent.parent_agent_id else agent.id
    with db.session() as s:
        rows = list(s.exec(select(EvalCase).where(EvalCase.status == "active")
                           .order_by(EvalCase.updated_at.desc())).all())
    picked = [c for c in rows if skill_name in (c.skill_names or [])] + \
             [c for c in rows if c.agent_id == owner and skill_name not in (c.skill_names or [])]
    return picked[: int(get_settings()["max_cases"])]


def queue_proposal_run(p: SkillProposal, cases: list[EvalCase]) -> EvalRun:
    run = EvalRun(trigger="proposal", proposal_id=p.id, case_ids=[c.id for c in cases],
                  variants=["without", "with"],
                  label=f"{p.agent_name}: {'improve' if p.base_version else 'new skill'} {p.name}")
    with db.session() as s:
        s.add(run)
        row = s.get(SkillProposal, p.id) or p
        row.eval_run_id = run.id
        row.eval_summary = {"status": "queued", "cases": len(cases)}
        s.add(row)
        s.commit()
        s.refresh(run)
    return run


def queue_run(case_ids: list[str], label: str = "") -> EvalRun:
    run = EvalRun(trigger="manual", case_ids=case_ids, variants=["current"], label=label)
    with db.session() as s:
        s.add(run)
        s.commit()
        s.refresh(run)
    return run


def _claim(run_id: str) -> bool:
    with db.session() as s:
        res = s.exec(update(EvalRun).where(EvalRun.id == run_id, EvalRun.status == "queued")
                     .values(status="running", started_at=_now()))
        s.commit()
        return (res.rowcount or 0) == 1


async def execute(rt, run_id: str) -> EvalRun:
    """Run a claimed run's cases (in each variant), grade them, and act on a proposal's."""
    with db.session() as s:
        run = s.get(EvalRun, run_id)
        cases = {c.id: c for c in s.exec(select(EvalCase).where(EvalCase.id.in_(run.case_ids))).all()}
        proposal = s.get(SkillProposal, run.proposal_id) if run.proposal_id else None
    cfg = get_settings()
    judge = cfg["judge_model"] or "each agent's own model"
    summary: dict[str, dict[str, int]] = {}
    try:
        for variant in run.variants:
            passed = total = 0
            for cid in run.case_ids:
                case = cases.get(cid)
                if case is None:
                    continue
                res = await run_case(rt, case, variant=variant, proposal=proposal, run_id=run_id)
                with db.session() as s:
                    s.add(res)
                    if variant in ("current", "with"):
                        row = s.get(EvalCase, cid)
                        if row is not None and variant == "current":
                            row.last_passed, row.last_run_at = res.passed, _now()
                            s.add(row)
                    s.commit()
                total += 1
                passed += int(res.passed)
            summary[variant] = {"passed": passed, "total": total}
        status, error = "done", None
    except Exception as e:
        log.exception("eval run %s failed", run_id)
        status, error = "failed", f"{type(e).__name__}: {e}"[:2000]
    with db.session() as s:
        row = s.get(EvalRun, run_id)
        row.status, row.error, row.summary, row.judge = status, error, summary, judge
        row.finished_at = _now()
        s.add(row)
        s.commit()
        s.refresh(row)
    if proposal is not None:
        await _proposal_outcome(rt, proposal.id, row)
    return row


async def _proposal_outcome(rt, proposal_id: str, run: EvalRun) -> None:
    """Record the numbers on the proposal; an Auto-mode change that passed is applied, one
    that made things worse (or couldn't be tested) waits for you."""
    w, wo = run.summary.get("with", {}), run.summary.get("without", {})
    regressed = run.status != "done" or w.get("passed", 0) < wo.get("passed", 0)
    info = {"status": run.status, "with": w, "without": wo, "regressed": regressed, "run_id": run.id}
    with db.session() as s:
        p = s.get(SkillProposal, proposal_id)
        if p is None:
            return
        p.eval_summary = info
        testing = p.status == "testing"
        if testing and regressed:
            p.status = "pending"  # the user decides
        s.add(p)
        s.commit()
        s.refresh(p)
    if p.message_id:
        await rt.update_message(p.message_id, eval_summary=info, status=p.status)
    if testing and not regressed:
        try:
            await rt.decide_skill_proposal(p.id, True, note="Applied: it didn't lower the pass rate.",
                                           by=f"{p.agent_name} (auto, tested)")
            with db.session() as s:
                row = s.get(SkillProposal, p.id)
                row.status = "applied"
                s.add(row)
                s.commit()
            if p.message_id:
                await rt.update_message(p.message_id, status="applied")
        except Exception as e:
            log.warning("could not apply tested proposal %s: %s", p.id, e)
    elif testing:
        from . import subagents

        await subagents.notify({"type": "skill_proposal", "proposal_id": p.id, "agent": p.agent_name,
                                "skill": p.name, "title": p.title, "revision": bool(p.base_version),
                                "reason": f"Held for review: tests passed {w.get('passed', 0)}/{w.get('total', 0)} "
                                          f"with the change vs {wo.get('passed', 0)}/{wo.get('total', 0)} without.",
                                "link": subagents.link(f"/requests/skills?skill={p.id}")})


async def run_due(rt, limit: int = 2) -> list[EvalRun]:
    with db.session() as s:
        queued = list(s.exec(select(EvalRun).where(EvalRun.status == "queued")
                             .order_by(EvalRun.created_at).limit(limit)).all())
    done = []
    for r in queued:
        if _claim(r.id):
            done.append(await execute(rt, r.id))
    return done


def prune() -> None:
    days = int(get_settings()["keep_traces_days"])
    with db.session() as s:
        s.exec(delete(JobTrace).where(JobTrace.created_at < _now() - timedelta(days=days)))
        s.commit()


def recover_stuck() -> None:
    with db.session() as s:
        s.exec(update(EvalRun).where(EvalRun.status == "running").values(status="queued"))
        s.commit()


async def loop(rt, every: float = 15) -> None:
    """Background: run queued test runs; forget old full tool results."""
    recover_stuck()
    last_prune = 0.0
    while True:
        try:
            if await rt.broker.r.set("ahq:evals:runner", "1", nx=True, ex=3600):
                try:
                    await run_due(rt)
                finally:
                    await rt.broker.r.delete("ahq:evals:runner")
            if time.monotonic() - last_prune > 3600:
                prune()
                last_prune = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("eval runner failed")
        await asyncio.sleep(every)
