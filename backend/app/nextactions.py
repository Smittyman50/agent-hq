"""Next actions for chat requests: break a request into steps and work them one at a time.

A local model given a big request in one run ("build me a Zork-style game") has to plan,
remember and do everything in one growing context, on one budget of model requests. It
runs out, or loses the thread, and falls back to its cloud model. Given the same request
as a standing goal it does fine, because the goal board breaks the work into next actions
and every turn starts from the card instead of a long transcript. This module brings that
way of working to ordinary requests, without the board or your approval:

1. **Gate.** Short replies, events, reports, goal turns, goal-team actions and sub-agents
   never plan (wants_plan). Everything else gets one quick planning request (no tools,
   less thinking) that answers DIRECT, or a brief of the request plus 2 to
   AHQ_PLAN_MAX_STEPS next actions (draft, parse).
2. **Steps.** Each next action runs as its own focused run on the agent's own model and
   tools, with a fresh request budget and no chat history: just the brief, the plan with
   earlier results, and its own step (step_prompt). Notes go on the job's scratch pad,
   shared by every step. A step that fails falls back to the fallback model for that step
   only (the usual fallback in Runtime._run_agent); the next step is back on the main model.
3. **Replanning.** A step that ends partial or failed, or says "PLAN:" in its result, gets a
   short revise request: KEEP, STOP, or new remaining steps (revise, parse_revision), at most
   AHQ_PLAN_MAX_REVISIONS times per request.
4. **Restarts.** When the worker running a request shuts down (a deploy) or dies, the job
   is picked up again by a worker and carries on with the same plan: finished steps keep
   their results, the interrupted step runs again (told to check what it already made),
   and nothing is planned again (resume). A step that can't reach its tool server is
   tried again after a wait (STEP_RETRIES) instead of failing. Only Stop ends a plan early.
5. **Reply.** A last run with the conversation's history writes the reply (or the
   WorkResult, for a delegated task) from the results (final_prompt). That's what the
   conversation's history keeps, so follow-ups know what was done.

Each agent has a mode (AgentDef.next_actions): auto (default), always, off. AHQ_NEXT_ACTIONS=0
turns planning off everywhere.

Plans live in Valkey only, like scratch pads, and show live in the chat's Next actions panel:
  ahq:plan:<job_id>         JSON: the plan (see new_plan)
  ahq:plans:<thread_id>     sorted set: job IDs with a plan in this conversation, by start
Kept while the job runs and AHQ_PAD_TTL_HOURS after it finishes, then they expire.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger("agent_hq.nextactions")

MODES = ("auto", "always", "off")
RUNNING_TTL = 7 * 24 * 3600
MIN_WORDS = 3          # auto: messages shorter than this never plan ("yes", "thanks!")
PLAN_TIMEOUT = 120     # seconds for one planning or revise request
CLIP_RESULT = 1500     # characters of a step's details kept on the plan
FINISHED = ("done", "partial", "failed", "skipped", "stopped")
# Plans a job picked up again after a worker restart carries on with (see resume).
UNFINISHED = ("working", "writing", "interrupted")
RESUMABLE = UNFINISHED + ("done", "partial", "failed")  # not "stopped": that was you
STEP_RETRIES = 2       # a step whose tool server can't be reached is tried again this often,
STEP_RETRY_WAIT = 20   # waiting this many seconds more each time (a container being recreated)

PLAN_RULES = """\
You plan next actions for {name}, an AI agent, before it starts on a request.

Decide whether the request needs several separate steps.
{direct_rule}
Otherwise break it into next actions: 2 to {max_steps} concrete steps, in order, each one
something {name} can finish in one focused run with its tools, and each producing something
the next can build on (a file, a finding, a decision). Name the files and outputs. Don't add
steps for reading the request, planning, or reporting back: the reply is written after the
last step automatically.

Reply in exactly this format and nothing else:
BRIEF: <the request in one or two sentences that stand on their own, with anything from the conversation it depends on>
1. <short step title> | <what to do, and what done looks like>
2. <short step title> | <what to do, and what done looks like>"""

DIRECT_AUTO = """\
Answer DIRECT (that one word) when one focused run handles it: a question, a quick lookup,
a short answer or message, one small change, chit-chat, or a follow-up to the last reply."""

DIRECT_ALWAYS = """\
Answer DIRECT (that one word) only for chit-chat or a one-line answer; plan everything else."""

REVISE_RULES = """\
You keep the plan of next actions for {name}, an AI agent working through a request one
step at a time. A step just finished. Decide whether the remaining steps still fit.

Reply with exactly one of:
KEEP
  (go on with the remaining steps as they are)
STOP <why, in one sentence>
  (the request can't be finished: a step it depends on failed and can't be worked around)
or the new remaining steps, numbered from 1, in this format (they replace all remaining steps):
1. <short step title> | <what to do, and what done looks like>
At most {max_steps} steps. Don't repeat steps that are done."""

STEP_RULES = """\
You're working through a request as next actions, one step at a time. This run is one
step: do it completely with your tools, and only it; later steps run after you. The plan in
your prompt is your memory of the request: earlier steps' results are in it, their notes are
on your scratch pad (read_notes), and files they made are still where they left them. Write
on your scratch pad anything a later step will need. Then return a WorkResult: status done,
partial or failed; summary: what you did and what it produced, in a few sentences with file
names; details: anything the next steps need. If what you learned changes the plan (a later
step isn't needed, or a new one is), start a line of the summary with "PLAN:" and say how."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def mode_of(agent) -> str:
    m = (getattr(agent, "next_actions", None) or "auto").lower()
    return m if m in MODES else "auto"


def wants_plan(agent, job, prompt: str, *, replay: Any = None) -> bool:
    """Whether this run may plan at all (no model call): a request from you or a schedule,
    or a delegated task, on an agent with planning on."""
    if not settings.next_actions_enabled or job is None or replay is not None:
        return False
    mode = mode_of(agent)
    if mode == "off" or getattr(agent, "is_subagent", False):
        return False
    meta = job.meta or {}
    kind = getattr(job.kind, "value", job.kind)
    if kind == "turn":
        if meta.get("kind") in ("goal", "mqtt", "webhook") or meta.get("everyone"):
            return False
        # Your messages carry message_id; reports, decisions and wake-ups don't.
        if not meta.get("message_id") and meta.get("kind") != "schedule":
            return False
    elif kind == "task":
        if meta.get("goal_action"):  # one action of a goal already
            return False
    else:
        return False
    if mode == "auto" and len((prompt or "").split()) < MIN_WORDS:
        return False
    return True


# --------------------------------------------------------------------------- parsing

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_STEP = re.compile(r"^\s*(?:[-*]\s*)?(?:step\s*)?(\d{1,2})\s*[.):]\s+(.+?)\s*$", re.I)


def _clean(text: str) -> str:
    text = _THINK.sub("", text or "")
    return text.replace("**", "").replace("`", "")


def _steps(text: str, max_steps: int) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        m = _STEP.match(line)
        if not m:
            if out and line.strip() and not line.strip().upper().startswith("BRIEF:"):
                out[-1]["details"] = (out[-1]["details"] + " " + line.strip()).strip()
            continue
        body = m.group(2).strip()
        title, _, details = body.partition("|")
        if not details:
            for sep in (" — ", " -- ", ": "):
                if sep in title and len(title.split(sep, 1)[0]) <= 80:
                    title, _, details = title.partition(sep)
                    break
        title = title.strip(" .")
        if len(title) > 90:
            details = (title + " " + details).strip()
            title = title[:87].rsplit(" ", 1)[0] + "…"
        if title:
            out.append({"title": title, "details": details.strip()})
    return out[:max_steps]


def parse(text: str, max_steps: int | None = None) -> dict | None:
    """The planner's answer: {brief, steps} with at least two steps, or None (DIRECT, or
    nothing usable, which is treated the same: the request runs as one run)."""
    max_steps = max_steps or settings.plan_max_steps
    text = _clean(text).strip()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if re.match(r"^\W*direct\b", first, re.I):
        return None
    brief = ""
    m = re.search(r"^\s*BRIEF\s*:\s*(.+)$", text, re.M | re.I)
    if m:
        brief = m.group(1).strip()
    steps = _steps(text, max_steps)
    if len(steps) < 2:
        return None
    return {"brief": brief, "steps": steps}


def parse_revision(text: str, max_steps: int | None = None) -> dict:
    """{"keep": True} | {"stop": why} | {"steps": [...]}. Anything unreadable keeps the plan."""
    max_steps = max_steps or settings.plan_max_steps
    text = _clean(text).strip()
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if re.match(r"^\W*stop\b", first, re.I):
        return {"stop": re.sub(r"^\W*stop\b[\s:.-]*", "", first, flags=re.I).strip() or "no reason given"}
    steps = _steps(text, max_steps)
    if steps and not re.match(r"^\W*keep\b", first, re.I):
        return {"steps": steps}
    return {"keep": True}


# --------------------------------------------------------------------------- prompts

def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def conversation_brief(history: list | None, *, turns: int = 6, clip: int = 600) -> str:
    """The last few things said in the conversation, for the planner."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    lines: list[str] = []
    for m in history or []:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, UserPromptPart):
                    c = p.content if isinstance(p.content, str) else " ".join(
                        x for x in p.content if isinstance(x, str))
                    lines.append(f"User: {_clip(c, clip)}")
        elif isinstance(m, ModelResponse):
            text = " ".join(p.content for p in m.parts if isinstance(p, TextPart)).strip()
            if text:
                lines.append(f"Agent: {_clip(text, clip)}")
    return "\n".join(lines[-turns:])


def planner_prompt(prompt: str, *, about: str, recent: str) -> str:
    parts = [about] if about else []
    if recent:
        parts.append("The conversation so far (latest last):\n" + recent)
    parts.append("The request:\n" + _clip(prompt, 6000))
    return "\n\n".join(parts)


def _plan_lines(plan: dict, current: int | None = None, *, results: bool = True) -> str:
    rows = []
    for s in plan["steps"]:
        tag = "now" if s["n"] == current else {"pending": "to do"}.get(s["status"], s["status"])
        row = f"{s['n']}. [{tag}] {s['title']}"
        if results and s.get("summary") and s["status"] in FINISHED:
            row += f"\n   Result: {_clip(s['summary'], 600)}"
        rows.append(row)
    return "\n".join(rows)


def step_prompt(plan: dict, step: dict, request: str) -> str:
    done = [s for s in plan["steps"] if s["status"] in FINISHED and s["n"] < step["n"]]
    carried = ""
    if done and done[-1].get("details"):
        carried = f"\n\nWhat step {done[-1]['n']} left for the next steps:\n{_clip(done[-1]['details'], CLIP_RESULT)}"
    again = ""
    if step.get("restarts"):
        again = ("\n\n[This step was started before and interrupted (a worker restart or a tool "
                 "server that couldn't be reached). Files and notes from that attempt may already "
                 "be there: check your workbench and read_notes, keep what's done and finish the rest.]")
    return (f"[Next actions: step {step['n']} of {len(plan['steps'])}]\n"
            f"Request: {plan.get('brief') or _clip(request, 1500)}\n\n"
            f"The user's message:\n{_clip(request, 3000)}\n\n"
            f"Plan:\n{_plan_lines(plan, step['n'])}{carried}\n\n"
            f"Your step now: {step['n']}. {step['title']}\n{step.get('details') or ''}".rstrip() + again)


def revise_prompt(plan: dict, step: dict) -> str:
    return (f"Request: {plan.get('brief')}\n\nPlan:\n{_plan_lines(plan)}\n\n"
            f"Step {step['n']} ({step['title']}) just finished: {step['status']}.\n"
            f"{_clip(step.get('summary') or '', 800)}\n{_clip(step.get('details') or '', 800)}").strip()


def final_prompt(request: str, plan: dict, *, worker: bool) -> str:
    rows = []
    for s in plan["steps"]:
        row = f"{s['n']}. [{s['status']}] {s['title']}"
        if s.get("summary"):
            row += f": {_clip(s['summary'], 900)}"
        if s.get("details"):
            row += f"\n   {_clip(s['details'], 900)}"
        rows.append(row)
    why = f"\nThe plan stopped early: {plan['stopped_because']}" if plan.get("stopped_because") else ""
    to = ("Return your WorkResult to whoever delegated this" if worker
          else "Write your reply to the user")
    return (f"{request}\n\n[You worked through this request as next actions. Results:\n"
            + "\n".join(rows) + why +
            f"\n{to} now, from these results; your scratch pad has the notes (read_notes). "
            "Don't redo the work: use tools only to check or fix something small, or to send "
            "the files the steps made that the reply should include. If a step failed or was "
            "skipped, say what's missing.]")


# --------------------------------------------------------------------------- plans

def new_plan(*, job, agent, draft: dict, depth: int) -> dict:
    now = _now()
    return {
        "job_id": job.id, "thread_id": job.origin_thread_id, "agent_id": agent.id,
        "agent": agent.name, "parent_job_id": job.parent_job_id, "depth": depth,
        "kind": getattr(job.kind, "value", job.kind),
        "brief": draft.get("brief") or "", "status": "working", "revisions": 0,
        "stopped_because": "", "created_at": now, "updated_at": now,
        "steps": [{"n": i, "title": s["title"], "details": s.get("details", ""),
                   "status": "pending", "summary": "", "started_at": None, "finished_at": None}
                  for i, s in enumerate(draft["steps"], 1)],
    }


def interrupt(plan: dict) -> None:
    """The worker is going away mid-plan: leave the plan to be resumed."""
    for s in plan["steps"]:
        if s["status"] == "running":
            s["status"] = "pending"
            s["restarts"] = s.get("restarts", 0) + 1
    if plan["status"] != "writing":
        plan["status"] = "interrupted"
    plan["interrupted"] = plan.get("interrupted", 0) + 1


def resume(plan: dict) -> None:
    """A job picked up again: carry on from the first step that hasn't finished. A step
    left running (the worker died without a word) runs again."""
    for s in plan["steps"]:
        if s["status"] == "running":
            s["status"] = "pending"
            s["restarts"] = s.get("restarts", 0) + 1
    if plan["status"] in ("working", "interrupted"):
        plan["status"] = "working"
    elif plan["status"] in ("done", "partial", "failed"):
        plan["status"] = "writing"  # the steps finished; the reply never got sent
    plan["resumed"] = plan.get("resumed", 0) + 1


def next_step(plan: dict) -> dict | None:
    return next((s for s in plan["steps"] if s["status"] == "pending"), None)


def replace_remaining(plan: dict, steps: list[dict]) -> None:
    kept = [s for s in plan["steps"] if s["status"] != "pending"]
    room = max(0, settings.plan_max_steps - len(kept))
    for i, s in enumerate(steps[:room], len(kept) + 1):
        kept.append({"n": i, "title": s["title"], "details": s.get("details", ""),
                     "status": "pending", "summary": "", "started_at": None, "finished_at": None})
    plan["steps"] = kept
    plan["revisions"] += 1


def skip_remaining(plan: dict, status: str = "skipped") -> None:
    for s in plan["steps"]:
        if s["status"] in ("pending", "running"):
            s["status"] = status


def outcome(plan: dict) -> str:
    st = [s["status"] for s in plan["steps"]]
    if all(x == "done" for x in st):
        return "done"
    return "partial" if "done" in st else "failed"


def _key(job_id: str) -> str:
    return f"ahq:plan:{job_id}"


def _index(thread_id: str) -> str:
    return f"ahq:plans:{thread_id}"


async def save(r, plan: dict, *, ttl: int | None = None) -> None:
    plan["updated_at"] = _now()
    ttl = ttl or RUNNING_TTL
    await r.set(_key(plan["job_id"]), json.dumps(plan), ex=ttl)
    idx = _index(plan["thread_id"])
    await r.zadd(idx, {plan["job_id"]: time.time()}, nx=True)
    await r.expire(idx, max(ttl, int(await r.ttl(idx) or 0)))


async def load(r, job_id: str) -> dict | None:
    raw = await r.get(_key(job_id))
    return json.loads(raw) if raw else None


async def for_thread(r, thread_id: str, limit: int = 20) -> list[dict]:
    """The conversation's plans still kept, oldest first."""
    ids = await r.zrange(_index(thread_id), -limit, -1)
    out, gone = [], []
    for jid in ids:
        p = await load(r, jid)
        (out.append(p) if p else gone.append(jid))
    if gone:
        await r.zrem(_index(thread_id), *gone)
    return out


def finished_ttl() -> int:
    return max(60, int(settings.pad_ttl_hours * 3600))
