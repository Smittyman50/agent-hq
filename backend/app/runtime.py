"""The agent runtime: building agents from data, running turns and delegated tasks.

Work flows through Valkey streams as Job records:

* A **turn** job runs one turn of a conversation, using the thread owner's agent.
  Your messages, MQTT events and background-job reports all become turn jobs.
* A **task** job is a delegation. The assignee returns a structured WorkResult.

Any worker on any machine can run a job: agent definitions and job context live in
the shared database, conversation history lives in the shared LangGraph checkpointer,
and live activity goes out over Valkey pub/sub.

Delegation contract (this is what guarantees results come back):

* `delegate(..., background=False)` queues a task job and waits for its WorkResult,
  which comes back as the tool's return value. While it waits, the delegator gives
  up its worker slot so the delegate can run even on a single-slot worker.
* If the wait exceeds AHQ_DELEGATE_WAIT_TIMEOUT, or `background=True` was used, the
  job is a background job: when it finishes, a report turn is queued in the
  conversation the work came from, waking the conversation's owner to tell you.
* Failures, model errors and jobs abandoned after repeated worker crashes all produce
  a `failed` WorkResult, which is reported the same way.
"""

from __future__ import annotations

import asyncio
import re
import contextlib
import dataclasses
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from pydantic_ai import (
    ToolReturn,
    Agent,
    FunctionToolset,
    ModelMessagesTypeAdapter,
    PrefixedToolset,
    RunContext,
    WrapperToolset,
    ModelRetry,
    capture_run_messages,
)
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.exceptions import (IncompleteToolCall, ModelRequestTimeout, ModelTimeoutError,
                                    OutputTokenLimitExceeded)
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (ModelMessage, ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart,
                                  UserPromptPart)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits
from redis.exceptions import LockError
from sqlmodel import select

from . import attachments as att
from . import knowledge_proposals as kprop
from . import approvals, clock, context, db, evals, knowledge, learning, profile, skills, slots, subagents, thinking, titles
from . import resources, workitems
from .broker import Broker
from .config import settings
from .llm import build_agent_model, build_model_for, describe_model
from . import memory
from .telemetry import current_traceparent, job_span, set_output
from .models import (
    AgentDef,
    Job,
    JobTrace,
    JobKind,
    JobStatus,
    Message,
    Role,
    LearningMode,
    MemoryKind,
    ProfileProposal,
    KnowledgeCollection,
    KnowledgeProposal,
    SkillProposal,
    SubagentMode,
    ToolApproval,
    SubagentRequest,
    Thread,
    ToolServer,
    Transport,
)

log = logging.getLogger("agent_hq.runtime")

# The worker slot (semaphore) of the job running in the current task, if any.
slot_var: ContextVar[asyncio.Semaphore | None] = ContextVar("ahq_slot", default=None)


@contextlib.asynccontextmanager
async def released_slot():
    """Give the current worker slot back while waiting on another job."""
    sem = slot_var.get()
    if sem is None:
        yield
        return
    sem.release()
    try:
        yield
    finally:
        await sem.acquire()


# --------------------------------------------------------------------------- types


MAX_FILES_PER_JOB = 20  # files one agent run may attach


class WorkResult(BaseModel):
    """What every delegated agent hands back to whoever delegated to it."""

    status: Literal["done", "partial", "failed"] = Field(
        description="done = task complete; partial = some progress; failed = could not do it")
    summary: str = Field(description="A few sentences the delegator can relay to the user")
    details: str = Field(default="", description="Supporting findings, data, links or logs")


@dataclass
class RunDeps:
    thread_id: str              # the conversation this work belongs to
    agent_id: str               # the agent currently running
    depth: int = 0              # 0 = the agent you are chatting with
    chain: list[str] = field(default_factory=list)  # agent IDs in the current chain
    job_id: str | None = None   # the job being executed
    # The tool calls made in this run, kept with the job so a reflection can see them.
    trace: list[dict] = field(default_factory=list)
    # In a test run (app/evals.py): answers tool calls from a recording instead of your lab.
    replay: Any = None
    # Files given with this turn or task (Attachment IDs), see app/attachments.py.
    attachment_ids: list[str] = field(default_factory=list)
    # Work started by an outside event (a webhook): tools that aren't marked read-only ask
    # first, even where the rules say allow. Passed on to delegated work and report-backs.
    cautious: bool = False


def _brief(value: Any, n: int = 300) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= n else text[:n] + "…"


def save_trace(job_id: str | None, trace: list[dict]) -> None:
    """Keep a job's tool calls with the job (the last 60, abbreviated) and in full in a
    JobTrace (for a couple of weeks) so the reply can become a test case."""
    if not job_id or not trace:
        return
    try:
        brief = [{k: v for k, v in t.items() if not k.endswith("_full")} for t in trace[-60:]]
        full = [{"tool": t["tool"], "args": t.get("args_full", {}), "result": t.get("result_full"),
                 "error": t.get("error")} for t in trace[-100:]]
        with db.session() as s:
            job = s.get(Job, job_id)
            if job is not None:
                job.meta = {**(job.meta or {}), "trace": brief, "tool_calls": len(trace)}
                s.add(job)
            row = s.get(JobTrace, job_id) or JobTrace(job_id=job_id)
            row.steps = full
            s.add(row)
            s.commit()
    except Exception as e:  # never let bookkeeping break a run
        log.warning("could not save the tool trace of job %s: %s", job_id, e)


GENERIC_RULES = """\
Operating rules:
- You may delegate to the team members listed below with the `delegate` tool. Pick by
  their descriptions. Write the task so it stands on its own: they do not see this chat.
- By default `delegate` waits and returns the team member's result to you. Use
  background=true only for long work; you will be woken in this conversation with the
  result when it finishes.
- You are accountable for delegated work: always report its outcome (including failures)
  back to whoever asked you.
- Make clear who did the work. Say whom you delegated to and what they reported, in your
  own words ("I asked Scarlett to check the feeds; she found..."). Never present a team
  member's result, or their failure, as if you had done it yourself. If they failed, say
  so, and suggest a next step if you can.
- Report only what your team actually returned (or what you know yourself). Never invent
  articles, sources, links, quotes, figures or dates to fill a gap. If you got fewer
  results than were asked for, say how many and stop there.
- If a team member fails at the same task twice, don't keep re-delegating it: report what
  happened and what you'd suggest instead.
- Keep the user's actual question in the task. If it's about what something looks like (a
  picture, a chart, a screen), ask for the image itself to be examined, not only the text.
"""


# --------------------------------------------------------------------------- data helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_agent(agent_id: str) -> AgentDef:
    with db.session() as s:
        a = s.get(AgentDef, agent_id)
        if a is None:
            raise LookupError(f"agent {agent_id} not found")
        return a


def load_job(job_id: str) -> Job | None:
    with db.session() as s:
        return s.get(Job, job_id)


def update_job(job_id: str, **fields: Any) -> Job:
    with db.session() as s:
        job = s.get(Job, job_id)
        for k, v in fields.items():
            setattr(job, k, v)
        s.add(job)
        s.commit()
        s.refresh(job)
        return job


def allowed_delegates(agent: AgentDef) -> list[AgentDef]:
    if not agent.delegates:
        return []
    with db.session() as s:
        rows = s.exec(select(AgentDef).where(AgentDef.enabled == True,  # noqa: E712
                                             AgentDef.is_subagent == False)).all()  # noqa: E712
    if "*" in agent.delegates:
        return [r for r in rows if r.id != agent.id]
    return [r for r in rows if r.id in agent.delegates and r.id != agent.id]


def find_delegate(agent: AgentDef, name: str) -> AgentDef | None:
    key = name.strip().lower()
    for d in allowed_delegates(agent):
        if d.name.lower() == key or d.id == name:
            return d
    return None


def mcp_toolset(srv: ToolServer) -> MCPToolset:
    if srv.transport == Transport.http:
        if not srv.url:
            raise ValueError(f"tool server {srv.name} has no URL")
        if srv.headers:
            from .webhooks import unseal

            return MCPToolset(srv.url, id=srv.name,
                              headers={k: unseal(v) for k, v in srv.headers.items()})
        return MCPToolset(srv.url, id=srv.name)
    if not srv.command:
        raise ValueError(f"tool server {srv.name} has no command")
    config = {"mcpServers": {srv.name: {"command": srv.command, "args": srv.args, "env": srv.env}}}
    return MCPToolset(config, id=srv.name)


def agent_tool_servers(agent: AgentDef) -> list[ToolServer]:
    """The servers ticked on the agent, then those every agent gets (all_agents)."""
    with db.session() as s:
        servers = [s.get(ToolServer, i) for i in agent.tool_server_ids or []]
        ticked = {srv.id for srv in servers if srv is not None}
        servers += [srv for srv in s.exec(select(ToolServer).where(ToolServer.all_agents == True)  # noqa: E712
                                          .order_by(ToolServer.name)).all()
                    if srv.id not in ticked and not srv.desktop]
    return [srv for srv in servers if srv is not None and srv.enabled]


def desktop_servers(agent: AgentDef) -> list[ToolServer]:
    return [srv for srv in agent_tool_servers(agent) if srv.desktop]


class ToolServerUnreachable(RuntimeError):
    """An agent's tool server could not be reached when a run started."""

    def __init__(self, server: str, where: str, reason: str) -> None:
        super().__init__(f"Can't reach tool server {server} ({where}): {reason}. "
                         "Check that it is running, or untick it on the agent.")
        self.server = server


def _root_reason(exc: BaseException) -> str:
    """The innermost message of an exception chain (e.g. All connection attempts failed)."""
    seen: set[int] = set()
    e: BaseException | None = exc
    last = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        last = e
        e = e.__cause__ or e.__context__
    return str(last) or type(last).__name__


@dataclass
class ReachableToolset(WrapperToolset):
    """Names the tool server when connecting to it fails, instead of a bare connect error."""

    server_name: str = ""
    where: str = ""

    async def __aenter__(self):
        try:
            return await super().__aenter__()
        except Exception as e:
            raise ToolServerUnreachable(self.server_name, self.where, _root_reason(e)) from e


def build_tool_toolsets(agent: AgentDef, wrap=None) -> list[tuple[Any, str | None]]:
    """[(toolset, desktop server id or None)] for the agent's MCP servers.

    `wrap(toolset, server)` is applied to each server's toolset before its tool names are
    prefixed (the approval check uses it, so it sees the server's own tool names).
    """
    out = []
    for srv in agent_tool_servers(agent):
        ts = ReachableToolset(mcp_toolset(srv), server_name=srv.name,
                              where=srv.url or srv.command or "no address")
        if wrap is not None:
            ts = wrap(ts, srv)
        # ahq_* tools are for Agent HQ itself (e.g. moving attachments to a workbench).
        shown = set(srv.shown_tools or [])
        ts = ts.filtered(lambda ctx, td, shown=shown: not td.name.startswith("ahq_")
                         and (not shown or td.name in shown))
        out.append((PrefixedToolset(ts, srv.name) if srv.prefix_tools else ts,
                    srv.id if srv.desktop else None))
    return out


DESKTOP_RULES = """Desktop handoff:
- If a web page needs a login, a CAPTCHA, an MFA code or anything else only the user
  should do, call `ask_user_to_take_over` with a short reason. It waits until the user
  hands the desktop back, then you continue. Never ask for passwords in chat.
- If a desktop tool says the user has control, stop using the desktop; tell whoever you
  report to that you are waiting for the user."""

VISION_RULES = """\
Seeing the screen:
- You cannot see screenshots yourself. Use `look` to ask questions about what is on the
  screen, and `locate` / `click_on` to act on things by describing them.
- For web pages, prefer the browser_* tools when you have them: call `browser_snapshot`
  to read the page as text and act on elements by their ref. Use `look` for things that
  are genuinely visual: charts and graphs, and photos or illustrations, especially when
  the question is what something looks like. Scroll the image into view first, then ask
  `look` a specific question about it."""

NO_VISION_RULES = """\
Images: no vision model is set for you. Unless your own model can view the screenshots
your tools return, you can only read text (page snapshots, alt text, captions). If a task
depends on what an image, photo, chart or the screen looks like and you can't view it,
report status "partial": give what the text says (captions and alt text count) and state
plainly that answering fully needs a vision model, which the user can set for you under
Settings → Agents. Don't report that something isn't there just because the text doesn't
mention it."""

ESCALATE_RULES = """\
If the task is beyond you (you keep failing, or it needs stronger reasoning), call
`escalate` with a short reason instead of guessing. A stronger model will take over."""


WORK_RULES = """\
Your turn ends the moment you reply without calling a tool, and nothing more happens until
the user writes again. So never end a reply with a plan or a promise ("I'll now build...",
"Next I'll...", "I'm starting with..."): make those tool calls now, in this same turn. Reply
in text only when the work is done, you are blocked, or you need the user to answer a
question; if you ask one, end your reply with it."""

# A text-only reply whose last paragraph announces work ("I'll now build ...") but asks
# nothing: the model meant to keep going and stopped. Offers ("if you want, I'll ...",
# "let me know") are fine endings and are not caught.
_PROMISE = re.compile(
    r"\b(i'?ll|i will|i'?m going to|i am going to|let me|i'?m (?:now )?(?:starting|beginning|about to)"
    r"|i am (?:now )?(?:starting|beginning)|next,? i|now i'?ll|starting with)\b", re.I)
_OFFER = re.compile(r"\b(if you(?:'d)? (?:like|want|prefer)|let me know|want me to|shall i|should i)\b", re.I)
CONTINUE_NUDGE = (
    "You said what you are going to do but made no tool call, so none of it has happened and "
    "the turn would end here. Do the work now by calling your tools. Reply in text only when "
    "it is finished, you are blocked, or you need the user to answer a question.")
TRUNCATED_NUDGE = (
    "Your reply was cut off: it reached the output limit for one response. Think less and "
    "take the next step now with a tool call, or give a shorter answer. Write big files in "
    "parts (one file per module, or append) rather than all at once.")
MAX_CONTINUE_NUDGES = 2  # under the agent's output retries (3), so a stubborn model still answers


# Markdown links the user can't open: sandbox:/work/game.py (ChatGPT's habit), file:...,
# or a bare path on the agent's workbench or desktop.
_DEAD_LINK = re.compile(r"\[[^\]]*\]\(\s*<?(?!(?:https?|mailto):|#)([^)\s>]+)")
FILE_LINK_NUDGE = (
    "Your reply links to files by path ({links}). The user can't open those: they only get files "
    "you send. Send each one with send_from_workbench (or create_file for text you write), then "
    "reply again without links to file paths; name the files instead.")


# Tools that change code, and tools that run it, by name (workbench tools come prefixed
# with their server's name, e.g. workbench-2_edit_file).
_CHANGES_CODE = ("write_file", "edit_file", "save_script")
_RUNS_CODE = ("run_python", "run_shell", "run_script")
# Changes to these don't need a run to prove them.
_NOT_CODE = (".md", ".txt", ".rst", ".csv", ".log")
# A reply may say plainly that it couldn't test; then it isn't sent back.
_SAYS_UNTESTED = re.compile(r"not testable|untested|(?:can'?t|cannot|couldn'?t|could not) (?:be )?(?:test|run)",
                            re.I)
UNTESTED_NUDGE = (
    "You changed {files} since you last ran anything, so this reply would report work you "
    "haven't checked. Run the code or its tests now (run_shell or run_python), read the "
    "result and fix what fails. If it can't run in the workbench, say so plainly in your "
    "reply and tell the user how to test it.")


def untested_changes(messages: list[ModelMessage]) -> list[str]:
    """Code files this turn changed after the last time it ran anything (empty: none)."""
    since = 0
    for i, m in enumerate(messages):
        if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts):
            since = i
    changed: list[str] = []
    for m in messages[since:]:
        if not isinstance(m, ModelResponse):
            continue
        for p in m.parts:
            if not isinstance(p, ToolCallPart):
                continue
            if p.tool_name.endswith(_RUNS_CODE):
                changed = []
            elif p.tool_name.endswith(_CHANGES_CODE):
                args = p.args_as_dict() if hasattr(p, "args_as_dict") else (p.args or {})
                path = str(args.get("path") or args.get("name") or "a file")
                if not path.lower().endswith(_NOT_CODE) and path not in changed:
                    changed.append(path)
    return changed


def dead_file_links(text: str) -> list[str]:
    """Links in a reply that point at files instead of web pages."""
    return _DEAD_LINK.findall(text or "")


def announces_unstarted_work(text: str) -> bool:
    """True when a final reply promises work it didn't do (see _PROMISE)."""
    body = (text or "").strip()
    if not body or body.endswith("?"):
        return False
    last = re.split(r"\n\s*\n", body)[-1]
    return bool(_PROMISE.search(last)) and not _OFFER.search(last)


def _resume_point(messages: list) -> list:
    """The messages of a failed run, cut back to the last complete exchange so another
    model can carry on: drop a trailing response whose tool calls never got results."""
    msgs = list(messages)
    while msgs and isinstance(msgs[-1], ModelResponse):
        msgs.pop()
    return msgs


class Escalation(Exception):
    """Raised by the escalate tool to hand the run to the agent's fallback model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _find_exc(exc: BaseException | None, kind: type) -> BaseException | None:
    """The first exception of `kind` in exc, its group members or its cause/context chain.

    Walks iteratively and visits each exception once: exception chains can loop (an
    ExceptionGroup re-raised "from" its own leaf's cause, as fastmcp's connection errors
    are), which made the old recursive version run until RecursionError and hide the
    real failure.
    """
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, kind):
            return e
        stack.append(e.__context__)
        stack.append(e.__cause__)
        stack.extend(reversed(list(getattr(e, "exceptions", ()) or ())))
    return None


def has_vision(agent: AgentDef) -> bool:
    return bool(agent.vision_model) and bool(desktop_servers(agent))


def memory_on(agent: AgentDef) -> bool:
    return agent.memory_enabled and memory.get_settings()["enabled"]


MEMORY_RULES = """\
Long-term memory:
- Relevant memories may be listed below. Treat them as leads to verify, not facts.
- Use `remember` for things worth knowing next time (how the environment is set up, what
  fixed a problem, the user's preferences). One clear, self-contained statement each.
  Never use it for notes about the task you're doing now or how you plan to answer.
- Use `forget` when you find a memory is wrong or outdated, `confirm` when you verify one,
  and `recall` to search for more."""


def has_fallback(agent: AgentDef) -> bool:
    return bool(agent.fallback_model)


def limit_settings(agent: AgentDef, *, on_fallback: bool = False) -> dict[str, Any]:
    """The agent's guardrails as model settings. They apply to its main model only: the
    fallback is what takes over when they trip."""
    if on_fallback:
        return {}
    out: dict[str, Any] = {}
    if agent.max_output_tokens:
        out["max_tokens"] = int(agent.max_output_tokens)
    if agent.request_timeout_s:
        # One deadline for the whole request, SDK retries included: a stuck local model
        # gets cut off once, not retried until three timeouts have passed.
        out["request_timeout"] = float(agent.request_timeout_s)
    return out


def ran_out(e: BaseException) -> str | None:
    """Why a run failed, when the cause is the model thinking or writing past a limit
    (rather than an error a second try on the same model can't fix)."""
    if _find_exc(e, IncompleteToolCall) is not None:
        return None  # a tool call cut off mid-arguments: thinking less won't shorten the file
    if _find_exc(e, OutputTokenLimitExceeded) is not None:
        return "it used up its output limit before answering"
    if _find_exc(e, ModelRequestTimeout) is not None or _find_exc(e, ModelTimeoutError) is not None:
        return "its request ran past the time limit"
    return None


DESKTOP_BUSY = ("This desktop is shared and {agent} is still using it, so it isn't free yet. "
                "Do not retry now. Report that the shared desktop is busy, or ask whoever you "
                "report to to try again later.")
DESKTOP_PAUSED = ("The user has taken control of this desktop, so desktop tools are paused. "
                  "Do not retry. Report that you are waiting for the user to hand the desktop "
                  "back; you will be woken when they do.")


def as_work_result(output: Any) -> WorkResult:
    """A delegated agent's output as a WorkResult (it may have answered in plain text)."""
    if isinstance(output, WorkResult):
        return output
    text = str(output).strip()
    if not text:
        return WorkResult(status="failed", summary="Finished without reporting anything.")
    return WorkResult(status="done", summary=text)


# Older tool results beyond these limits are shortened before each model call, so a long
# research run (searches, pages) doesn't overflow a local model's context window.
KEEP_RECENT_TOOL_RESULTS = 3
TRIM_TOOL_RESULT_TO = 1200
TRIM_WHEN_OVER_CHARS = 40_000


SHORTEN_CALL_ARG_OVER = 2000  # characters in one argument of an old tool call


def shorten_old_tool_calls(messages: list[ModelMessage], keep: int = KEEP_RECENT_TOOL_RESULTS,
                           limit: int = SHORTEN_CALL_ARG_OVER) -> list[ModelMessage]:
    """Shorten long arguments of all but the latest few tool calls.

    A coding agent's write_file call carries the whole file, and every rewrite stays in
    the history: a few rewrites of a 20k-character file fill a 32k window by themselves.
    Over the window, Ollama drops the oldest messages, the user's request with them, and
    Qwen's chat template then fails with 'No user query found in messages'. Those calls
    have already run, so the file is on disk and the model can read it back.
    """
    calls = [(i, j) for i, m in enumerate(messages) if isinstance(m, ModelResponse)
             for j, p in enumerate(m.parts) if isinstance(p, ToolCallPart)]
    out = list(messages)
    for i, j in (calls[:-keep] if keep else calls):
        p = out[i].parts[j]
        args = p.args
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if not isinstance(args, dict):
            continue
        short = {k: (v[:200] + f"… [{len(v):,} characters omitted to save space; this call already "
                               "ran, so read the result back with a tool if you need it]")
                 if isinstance(v, str) and len(v) > limit else v
                 for k, v in args.items()}
        if short != args:
            parts = list(out[i].parts)
            parts[j] = dataclasses.replace(p, args=short)
            out[i] = dataclasses.replace(out[i], parts=parts)
    return out


def trim_old_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """PydanticAI history processor: shorten all but the latest few large tool results,
    and the long arguments of all but the latest few tool calls."""
    messages = shorten_old_tool_calls(messages)
    def size(c: Any) -> int:
        return len(c) if isinstance(c, str) else len(json.dumps(c, default=str))

    returns = [(i, j) for i, m in enumerate(messages) if isinstance(m, ModelRequest)
               for j, p in enumerate(m.parts) if isinstance(p, ToolReturnPart)]
    total = sum(size(messages[i].parts[j].content) for i, j in returns)
    if total <= TRIM_WHEN_OVER_CHARS:
        return messages
    old = returns[:-KEEP_RECENT_TOOL_RESULTS] if KEEP_RECENT_TOOL_RESULTS else returns
    by_msg: dict[int, list[int]] = {}
    for i, j in old:
        by_msg.setdefault(i, []).append(j)
    out = list(messages)
    for i, parts_idx in by_msg.items():
        parts = list(out[i].parts)
        for j in parts_idx:
            p = parts[j]
            text = p.content if isinstance(p.content, str) else json.dumps(p.content, default=str)
            if len(text) > TRIM_TOOL_RESULT_TO:
                parts[j] = dataclasses.replace(p, content=text[:TRIM_TOOL_RESULT_TO] + (
                    f"… [shortened from {len(text)} characters to save space; call the tool "
                    "again if you need the rest]"))
        out[i] = dataclasses.replace(out[i], parts=parts)
    return out


def attributed(name: str, result: dict, pronouns: str = "") -> dict:
    """A delegate's result, labelled so the delegator relays it as theirs, not its own."""
    refer = f" Refer to {name} as {pronouns.strip()}." if pronouns.strip() else ""
    return {"from": name, **result,
            "note": f"This is {name}'s report to you, not your own work. Tell whoever asked "
                    f"you that you delegated this to {name} and what {name} reported.{refer}"}


def team_roster(agent: AgentDef) -> str:
    team = allowed_delegates(agent)
    if not team:
        return ""
    lines = [f"- {t.name}: {t.description or '(no description)'}"
             + (f" (pronouns: {t.pronouns.strip()})" if t.pronouns.strip() else "") for t in team]
    return GENERIC_RULES + "\nTeam members you can delegate to:\n" + "\n".join(lines)


SUBAGENT_SELF_RULES = """\
You are a temporary sub-agent, created by another agent for this one task. Do just this
task with the tools you have, then return your WorkResult. You can't delegate or create
sub-agents. Report only what you actually found."""


def subagent_rules(agent: AgentDef, mode: SubagentMode) -> str:
    """How this agent may use sub-agents, with its current options and limits."""
    lim = subagents.limits_of(agent)
    models = ", ".join(m["label"] for m in subagents.model_options(agent, lim)) or "(none set)"
    tools = ", ".join(t.name for t in subagents.tool_options(agent, lim)) or "none"
    team = ", ".join(a.name for a in subagents.defined_agents(agent.id)) or "nobody else"
    head = ("""Sub-agents:
- You may create temporary sub-agents with `spawn_subagent` for parts of your task that
  can run separately (e.g. reading several sources in parallel). Each gets one
  self-contained task and retires when it reports back.""" if mode == SubagentMode.allowed else
            """Sub-agents (need the user's approval):
- If part of your task really needs extra hands, call `request_subagents` explaining which
  sub-agents you want, why, why none of the existing agents fits, and roughly how long
  each needs. The user approves or denies it; you'll be told. Only after approval can you
  use `spawn_subagent`, within what was approved. Don't ask for routine work you can do
  yourself.""")
    limits = (f"Limits: {lim['concurrent']} at once, {lim['per_task']} per task, {lim['per_day']} per day, "
              f"{lim['max_minutes']} minutes and {lim['max_requests']} model requests each."
              if mode == SubagentMode.allowed else
              "Limits: whatever the user approves (shown in the approval).")
    return f"""{head}
- Never create a sub-agent for work an existing agent is defined for: delegate to them
  instead. Existing agents: {team}. Requests that duplicate one are refused.
- Models for sub-agents: {models}. Tool servers they can have: {tools}.
- {limits}
- You are accountable for sub-agents' work: check it, and tell whoever asked you that you
  used a sub-agent and what it found. Never present made-up results."""


class PlannedSubagent(BaseModel):
    """One sub-agent in a request_subagents request."""

    role: str = Field(description="Short role name, e.g. 'Feed reader'")
    purpose: str = Field(description="What it will do and what it returns")
    model: str | None = Field(default=None, description="One of the listed models (default: the first)")
    tools: list[str] | None = Field(default=None, description="Tool server names it needs (default: all you may give)")
    est_minutes: int = Field(default=10, description="Your estimate of how long it needs, in minutes")


SKILL_INTRO = """Skills (written-down know-how from earlier work):
- If one of the skills listed below fits the task, call `load_skill` with its name and
  follow it: it gives the steps and the pitfalls. `list_skills` searches all of them.
- Skills don't give you permissions: tool approvals still apply."""


APPROVAL_NOTE = """Approvals: the user may require approval for some tool calls. Such a call
waits for the user's decision. If a call comes back denied or not approved, don't try to
get around it (no other tool or wording for the same thing): adapt, or report that it
needs the user's approval. If a tool is blocked, it isn't available to you."""


@dataclass
class ApprovalToolset(WrapperToolset):
    """Applies the server's approval rules to each call: allow, ask the user, or block."""

    rt: Any = None
    server_id: str = ""
    server_name: str = ""
    rules: dict[str, str] | None = None
    read_only: bool = False  # you marked the whole server read-only

    eval_live: dict[str, bool] | None = None

    async def call_tool(self, name, tool_args, ctx: RunContext[RunDeps], tool):
        metadata = (getattr(tool, "tool_def", None) and tool.tool_def.metadata) or {}
        meta = metadata.get("meta")
        mode = approvals.mode_for(self.rules, name, meta)
        read_only = self.read_only or bool((metadata.get("annotations") or {}).get("readOnlyHint"))
        if ctx.deps.replay is not None:  # a test run: only Allow + safe-in-tests tools run for real
            live = (self.eval_live or {}).get(name, read_only)
            if mode != "allow" or not live or not ctx.deps.replay.live_allowed:
                return ctx.deps.replay.not_available(name, tool_args, mode, live)
            ctx.deps.replay.went_live(name)
            return await super().call_tool(name, tool_args, ctx, tool)
        if mode == "block":
            return (f"Not allowed: the user has blocked {name} on {self.server_name}. "
                    "Don't try to do the same thing another way; say what you needed it for.")
        reason = ""
        if mode == "allow" and ctx.deps.cautious and not read_only:
            mode = "ask"
            reason = "This work was started by a webhook, so tools that can change things ask first."
        if mode == "ask" and not approvals.remembered(ctx.deps.agent_id, ctx.deps.thread_id,
                                                      self.server_id, name):
            decision = await self.rt.ask_approval(ctx.deps, self.server_id, self.server_name,
                                                  name, tool_args, reason=reason)
            if decision["status"] != "approved":
                return decision["message"]
        return await super().call_tool(name, tool_args, ctx, tool)


@dataclass
class ObservedToolset(WrapperToolset):
    """Publishes every tool call and result to the conversation's live event stream."""

    agent_name: str = ""
    broker: Broker | None = None
    desktop_server_id: str | None = None
    # A shared desktop resource: one conversation at a time (app/resources.py).
    lease_resource_id: str | None = None
    source: str = "internal"  # internal (Agent HQ's own tools) | mcp (your tool servers)

    async def call_tool(self, name, tool_args, ctx: RunContext[RunDeps], tool):
        d = ctx.deps
        if d.replay is not None:
            inner = super().call_tool
            return await d.replay.call(name, tool_args, self.source,
                                       lambda: inner(name, tool_args, ctx, tool))
        if self.desktop_server_id and await self.broker.user_has_control(self.desktop_server_id):
            await self.broker.mark_blocked(self.desktop_server_id,
                                           {"agent_id": d.agent_id, "thread_id": d.thread_id})
            await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                      agent_id=d.agent_id, tool=name, depth=d.depth,
                                      error="paused: you have control of this desktop")
            return DESKTOP_PAUSED
        if self.lease_resource_id:
            busy = await self._take_desktop(d)
            if busy:
                await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                          agent_id=d.agent_id, tool=name, depth=d.depth,
                                          error=f"shared desktop busy: {busy.get('agent')} is using it")
                return DESKTOP_BUSY.format(agent=busy.get("agent") or "another agent")
        await self.broker.publish(d.thread_id, "tool_call", agent=self.agent_name,
                                  agent_id=d.agent_id, tool=name, args=tool_args, depth=d.depth)
        step = {"tool": name, "args": _brief(tool_args), "args_full": tool_args}
        d.trace.append(step)
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
        except Exception as e:
            step["error"] = _brief(str(e), 2000)
            await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                      agent_id=d.agent_id, tool=name, error=str(e), depth=d.depth)
            raise
        step["result"] = _brief(result)
        step["result_full"] = _brief(result, 8000)
        await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                  agent_id=d.agent_id, tool=name, preview=str(result)[:500],
                                  depth=d.depth)
        return result


    async def _take_desktop(self, d: RunDeps) -> dict | None:
        """Wait for the shared desktop; None once it's this conversation's, else who has it."""
        from . import resources

        async def waiting(cur: dict) -> None:
            await self.broker.publish(d.thread_id, "desktop_wait", agent=self.agent_name,
                                      agent_id=d.agent_id, depth=d.depth,
                                      holder=cur.get("agent"), resource_id=self.lease_resource_id)

        return await resources.acquire_lease(self.broker.r, self.lease_resource_id,
                                             thread_id=d.thread_id, job_id=d.job_id,
                                             agent=self.agent_name, on_wait=waiting)


# --------------------------------------------------------------------------- runtime


class TurnState(TypedDict, total=False):
    agent_id: str
    prompt: str
    attachments: list[str]
    job_id: str
    history: list[dict]
    reply: str


class Runtime:
    """Shared by the API process (which only enqueues) and workers (which execute).

    Pass a LangGraph checkpointer in processes that run turns (workers).
    """

    def __init__(self, broker: Broker, checkpointer=None) -> None:
        self.broker = broker
        slots.use(broker.r)  # provider slots are leases in this process's Valkey
        self.graph = None
        if checkpointer is not None:
            g = StateGraph(TurnState)
            g.add_node("run_agent", self._node_run_agent)
            g.add_edge(START, "run_agent")
            g.add_edge("run_agent", END)
            # The checkpointer persists each thread's model-level history (tool calls included).
            self.graph = g.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------ transcript

    async def save_message(self, thread_id: str, role: Role, content: str,
                           agent_id: str | None = None, **meta: Any) -> Message:
        m = Message(thread_id=thread_id, role=role, content=content, agent_id=agent_id, meta=meta)
        with db.session() as s:
            s.add(m)
            if role in (Role.user, Role.agent):  # the sidebar's "last updated"
                t = s.get(Thread, thread_id)
                if t is not None:
                    t.updated_at = m.created_at
                    s.add(t)
            s.commit()
            s.refresh(m)
        await self.broker.publish(thread_id, "message", message=json.loads(m.model_dump_json()))
        return m

    async def update_message(self, message_id: str, *, content: str | None = None,
                             **meta: Any) -> Message | None:
        """Change a message in place (e.g. a delegation going from working to done)."""
        with db.session() as s:
            m = s.get(Message, message_id)
            if m is None:
                return None
            if content is not None:
                m.content = content
            m.meta = {**(m.meta or {}), **meta}  # reassign so the JSON column is saved
            s.add(m)
            s.commit()
            s.refresh(m)
        await self.broker.publish(m.thread_id, "message_updated", message=json.loads(m.model_dump_json()))
        return m

    # ------------------------------------------------------------ enqueueing

    async def enqueue_turn(self, thread_id: str, prompt: str, *, from_user: bool = True,
                           event_text: str | None = None, attachments: list[str] | None = None,
                           **meta: Any) -> Job:
        """Queue one turn of a conversation for its owner agent.

        `attachments`: IDs of files you uploaded to this conversation for this message.
        """
        with db.session() as s:
            thread = s.get(Thread, thread_id)
        if thread is None:
            raise LookupError(f"thread {thread_id} not found")
        owner = load_agent(thread.agent_id)
        files = att.in_thread(thread_id, attachments or [])
        if from_user:
            learning.user_said(thread_id, prompt)  # a correction? the last job gets a look
            extra = {"attachments": [att.summary(a) for a in files]} if files else {}
            msg = await self.save_message(thread_id, Role.user, prompt, **extra)
            if files:
                att.link([a.id for a in files], msg.id)
        elif event_text:
            await self.save_message(thread_id, Role.event, event_text, **meta)
        task = prompt
        if files:
            task = ((prompt.strip() or "(The user sent these files without a message.)")
                    + "\n\n" + att.manifest(files, sees_images=owner.sees_images))
            meta = {**meta, "attachments": [a.id for a in files]}
        job = Job(kind=JobKind.turn, origin_thread_id=thread_id, assignee_agent_id=owner.id,
                  task=task, depth=0, chain=[owner.id], pool=owner.pool,
                  meta={**meta, **({"event": event_text} if event_text else {})})
        return await self._submit(job)

    async def _submit(self, job: Job) -> Job:
        job.trace_parent = job.trace_parent or current_traceparent()
        with db.session() as s:
            s.add(job)
            s.commit()
            s.refresh(job)
        await self.broker.enqueue(job.pool, job.id)
        await self.broker.publish(job.origin_thread_id, "job_queued", job_id=job.id,
                                  kind=job.kind, pool=job.pool, agent_id=job.assignee_agent_id)
        return job

    # ------------------------------------------------------------ execution (workers)

    async def execute(self, job_id: str, worker: str) -> None:
        """Run a job delivered to a worker. Safe to call again for a redelivered job.

        Returns normally when you stop the job (Stop in the UI), so the worker acks it;
        raises CancelledError only when the worker itself is shutting down or dying.
        """
        job = load_job(job_id)
        if job is None or job.status in (JobStatus.done, JobStatus.failed):
            return  # unknown, or already finished before a crash prevented the ack
        if await self.broker.stop_requested(job.id):
            await self._stopped(job)  # stopped while it waited in the queue
            return
        attempts = job.attempts + 1
        if attempts > settings.max_attempts:
            await self._give_up(job)
            return
        job = update_job(job.id, status=JobStatus.running, attempts=attempts, worker=worker,
                         started_at=_now())
        if attempts > 1:
            await self.broker.publish(job.origin_thread_id, "job_retry", job_id=job.id,
                                      attempt=attempts, worker=worker)
        stopped = asyncio.Event()
        work = asyncio.create_task(self._execute(job, worker))
        watch = asyncio.create_task(self._watch_stop(job.id, work, stopped))
        try:
            await work
        except asyncio.CancelledError:
            me = asyncio.current_task()
            shutting_down = bool(me and getattr(me, "cancelling", lambda: 0)())  # 3.11+
            if stopped.is_set() and not shutting_down:
                await self._stopped(load_job(job.id))  # you pressed Stop: a finished job
                return
            raise  # shutdown or crash: leave it for another worker
        finally:
            watch.cancel()

    async def _execute(self, job: Job, worker: str) -> None:
        assignee = load_agent(job.assignee_agent_id)
        with job_span(job, assignee.name, worker) as span:
            if job.kind == JobKind.turn:
                await self._run_turn(job)
            else:
                await self._run_task(job)
            set_output(span, (load_job(job.id).result or {}).get("reply")
                       or load_job(job.id).result)

    async def _watch_stop(self, job_id: str, work: asyncio.Task, stopped: asyncio.Event) -> None:
        """Cancel `work` once you press Stop. Polls, so a missed message can't lose a stop."""
        while not work.done():
            await asyncio.sleep(settings.stop_poll)
            try:
                if await self.broker.stop_requested(job_id):
                    stopped.set()
                    work.cancel()
                    return
            except Exception as e:  # Valkey hiccup: keep running, check again
                log.warning("stop check for job %s failed: %s", job_id, e)

    # ------------------------------------------------------------ stopping

    STOPPED = "Stopped by you."

    async def stop_thread(self, thread_id: str) -> dict:
        """Stop everything still queued or running in a conversation: the turn and every
        task delegated from it, on whichever workers they are. Running jobs stop within
        about AHQ_STOP_POLL seconds; queued ones never start."""
        with db.session() as s:
            jobs = s.exec(select(Job).where(
                Job.origin_thread_id == thread_id,
                Job.status.in_([JobStatus.queued, JobStatus.running]))).all()
        # Flags first: a worker that picks up a queued job after this sees the flag.
        for j in jobs:
            await self.broker.request_stop(j.id)
        for j in jobs:
            if j.status == JobStatus.queued:
                await self._stopped(load_job(j.id))
        if jobs:
            await self.save_message(thread_id, Role.event, self.STOPPED, kind="stopped",
                                    job_ids=[j.id for j in jobs])
        workitems.stopped(thread_id)  # a goal's conversation: don't let the runner restart it
        await self.broker.publish(thread_id, "status", state="idle")
        return {"stopped": len(jobs), "job_ids": [j.id for j in jobs]}

    async def _stopped(self, job: Job | None) -> None:
        """Record a job you stopped. Safe to call twice; the second call does nothing."""
        if job is None or job.status in (JobStatus.done, JobStatus.failed):
            return
        result = WorkResult(status="failed", summary=self.STOPPED)
        update_job(job.id, status=JobStatus.failed, error=self.STOPPED,
                   result=result.model_dump(), finished_at=_now())
        skills.finish_job(job.id, "stopped")
        learning.job_finished(job.id, "stopped")  # not a failure worth reflecting on
        await self.broker.publish(job.origin_thread_id, "job_stopped", job_id=job.id,
                                  kind=job.kind, agent_id=job.assignee_agent_id)
        if job.kind != JobKind.task:
            return
        assignee = load_agent(job.assignee_agent_id)
        requester = load_agent(job.requester_agent_id) if job.requester_agent_id else None
        await self.broker.publish(job.origin_thread_id, "delegation_finished", job_id=job.id,
                                  from_agent=requester.name if requester else None,
                                  to_agent=assignee.name,
                                  result={**result.model_dump(), "status": "stopped"},
                                  depth=job.depth)
        if (job.meta or {}).get("message_id"):
            await self.update_message(
                job.meta["message_id"],
                content=f"{assignee.name} → {requester.name if requester else 'you'}: [stopped] {self.STOPPED}",
                status="stopped", result={**result.model_dump(), "status": "stopped"},
                finished_at=_now().isoformat())
        if assignee.is_subagent:
            subagents.retire(assignee.id)
        if (job.meta or {}).get("goal_action"):
            workitems.team_stopped(job)  # its action goes back to do
        # Wake a delegator still waiting on it (normally stopped too). No report turn:
        # you stopped the work, so nobody should start talking about it.
        await self.broker.push_result(job.id, result.model_dump())

    async def _give_up(self, job: Job) -> None:
        msg = f"Abandoned after {settings.max_attempts} attempts (workers kept dying)."
        result = WorkResult(status="failed", summary=msg)
        update_job(job.id, status=JobStatus.failed, error=msg, result=result.model_dump(),
                   finished_at=_now())
        if job.kind == JobKind.turn:
            await self.save_message(job.origin_thread_id, Role.event, f"Error: {msg}", kind="error")
        else:
            await self._finish_task(job, result)

    # ------------------------------------------------------------ turns

    async def _node_run_agent(self, state: TurnState, config) -> TurnState:
        thread_id = config["configurable"]["thread_id"]
        agent = load_agent(state["agent_id"])
        history = ModelMessagesTypeAdapter.validate_python(state.get("history") or [])
        job = load_job(state["job_id"]) if state.get("job_id") else None
        deps = RunDeps(thread_id=thread_id, agent_id=agent.id, depth=0, chain=[agent.id],
                       job_id=state.get("job_id"), attachment_ids=list(state.get("attachments") or []),
                       cautious=bool(job and (job.meta or {}).get("cautious")))
        try:
            reply, messages = await self.run_agent(agent, state["prompt"], deps, history=history)
        finally:
            save_trace(deps.job_id, deps.trace)
        return {"reply": reply,
                "history": ModelMessagesTypeAdapter.dump_python(messages, mode="json")}

    async def _run_turn(self, job: Job) -> None:
        if self.graph is None:
            raise RuntimeError("this process has no checkpointer; it cannot run turns")
        thread_id = job.origin_thread_id
        lock = self.broker.thread_lock(thread_id, ttl=60)
        async with released_slot():  # don't hold a slot while queued behind another turn
            await lock.acquire()
        keeper = asyncio.create_task(self._keep_lock(lock))
        try:
            await self.broker.publish(thread_id, "status", state="thinking",
                                      agent_id=job.assignee_agent_id, job_id=job.id)
            out = await self.graph.ainvoke(
                # attachments always set: graph state carries over between turns otherwise
                {"agent_id": job.assignee_agent_id, "prompt": job.task, "job_id": job.id,
                 "attachments": list((job.meta or {}).get("attachments") or [])},
                config={"configurable": {"thread_id": thread_id}})
            reply = out["reply"]
            msg = await self.save_message(thread_id, Role.agent, reply, agent_id=job.assignee_agent_id,
                                          job_id=job.id)
            await att.link_outputs(self, job.id, msg.id)  # files the agent made this turn
            update_job(job.id, status=JobStatus.done, result={"reply": reply}, finished_at=_now())
            titles.title_soon(thread_id, self.broker)  # a chat still called "Chat with …" gets a name
            skills.finish_job(job.id, "done")
            learning.job_finished(job.id, "done")
            if (job.meta or {}).get("kind") in ("mqtt", "webhook"):
                await self._capture_event(job, reply)
        except Exception as e:
            log.exception("turn %s failed", job.id)
            await self.save_message(thread_id, Role.event, f"Error: {e}", kind="error")
            update_job(job.id, status=JobStatus.failed, error=str(e), finished_at=_now())
            skills.finish_job(job.id, "failed")
            learning.job_finished(job.id, "failed")
        finally:
            keeper.cancel()
            with contextlib.suppress(LockError):
                await lock.release()
            await self.broker.publish(thread_id, "status", state="idle",
                                      agent_id=job.assignee_agent_id)

    @staticmethod
    async def _keep_lock(lock) -> None:
        while True:
            await asyncio.sleep(20)
            with contextlib.suppress(LockError):
                await lock.extend(60, replace_ttl=True)

    # ------------------------------------------------------------ tasks

    async def _run_task(self, job: Job) -> None:
        assignee = load_agent(job.assignee_agent_id)
        requester = load_agent(job.requester_agent_id) if job.requester_agent_id else None
        deps = RunDeps(thread_id=job.origin_thread_id, agent_id=assignee.id, depth=job.depth,
                       chain=list(job.chain), job_id=job.id,
                       attachment_ids=list((job.meta or {}).get("attachments") or []),
                       cautious=bool((job.meta or {}).get("cautious")))
        await self.broker.publish(job.origin_thread_id, "delegation_started", job_id=job.id,
                                  from_agent=requester.name if requester else None,
                                  to_agent=assignee.name, task=job.task,
                                  background=job.background, depth=job.depth,
                                  worker=job.worker)
        if (job.meta or {}).get("message_id"):
            await self.update_message(job.meta["message_id"], status="working", worker=job.worker)
        if assignee.is_subagent and assignee.retired:
            result = WorkResult(status="failed", summary=f"{assignee.name} expired before a "
                                "worker could start it (the workers were busy). Try again later.")
            update_job(job.id, status=JobStatus.failed, error=result.summary,
                       result=result.model_dump(), finished_at=_now())
            await self._finish_task(job, result)
            return
        try:
            try:
                result, _ = await self.run_agent(assignee, job.task, deps, as_worker=True)
            finally:
                save_trace(job.id, deps.trace)
            result = as_work_result(result)
            made = att.made_by_job(job.id)
            if made:  # so the delegator knows the files exist and can use them
                listing = "\n".join(f"- {att.ref(a.id)}: {a.name} ({att.human_size(a.size)})" for a in made)
                result.details = (result.details + "\n\n" if result.details else "") + \
                    f"Files {assignee.name} attached (in this conversation):\n{listing}"
            update_job(job.id, status=JobStatus.done, result=result.model_dump(),
                       finished_at=_now())
        except Exception as e:
            log.exception("task %s failed", job.id)
            result = WorkResult(status="failed", summary=f"{assignee.name} hit an error: {e}")
            update_job(job.id, status=JobStatus.failed, error=str(e),
                       result=result.model_dump(), finished_at=_now())
        await self._finish_task(job, result)

    async def _finish_task(self, job: Job, result: WorkResult) -> None:
        team = bool((job.meta or {}).get("goal_action"))
        if team:
            # A goal team member: its result goes on its action (app/workitems.py) first,
            # since the job already shows as finished and the goal runner must not see a
            # finished team job whose action is still being worked.
            workitems.team_result(job, result.status, result.summary, result.details)
        skills.finish_job(job.id, result.status)
        learning.job_finished(job.id, result.status)
        assignee = load_agent(job.assignee_agent_id)
        requester = load_agent(job.requester_agent_id) if job.requester_agent_id else None
        await self.broker.publish(job.origin_thread_id, "delegation_finished", job_id=job.id,
                                  from_agent=requester.name if requester else None,
                                  to_agent=assignee.name, result=result.model_dump(),
                                  depth=job.depth)
        text = (f"{assignee.name} → {requester.name if requester else 'you'}: "
                f"[{result.status}] {result.summary}")
        message_id = (job.meta or {}).get("message_id")
        updated = message_id and await self.update_message(
            message_id, content=text, status=result.status, result=result.model_dump(),
            finished_at=_now().isoformat())
        if updated:
            await att.link_outputs(self, job.id, message_id)  # files the assignee made
        if not updated:  # a job queued before this version: no message yet
            await self.save_message(
                job.origin_thread_id, Role.event, text,
                agent_id=assignee.id, kind="delegation_result", job_id=job.id,
                result=result.model_dump(), status=result.status,
                # Both sides of the exchange, so the UI can show it as a thread between the two.
                task=job.task, requester_id=requester.id if requester else None,
                requester=requester.name if requester else None, assignee=assignee.name,
                background=job.background, depth=job.depth,
                started_at=job.created_at.isoformat() if job.created_at else None)
        await self._capture_delegation(job, assignee, result)
        if assignee.is_subagent:
            subagents.retire(assignee.id)  # one task each; frees the slot for the next
        # Wake a waiting delegator, if there is one.
        await self.broker.push_result(job.id, result.model_dump())
        # Background jobs (including waits that timed out) report into the origin thread.
        # Goal team results don't: the board is where the goal's agent reads them.
        if load_job(job.id).background and not team:
            await self.enqueue_turn(job.origin_thread_id, self._report_prompt(job, assignee, result),
                                    from_user=False, report_for=job.id,
                                    **({"cautious": True} if (job.meta or {}).get("cautious") else {}))

    @staticmethod
    def _report_prompt(job: Job, assignee: AgentDef, result: WorkResult) -> str:
        return (f"[Background job {job.id} finished]\n"
                f"{assignee.name} was asked: {job.task}\n"
                f"Result: {result.model_dump_json()}\n\n"
                + (f"({assignee.name}'s pronouns: {assignee.pronouns.strip()})\n"
                   if assignee.pronouns.strip() else "")
                + f"Report this outcome to the user now as {assignee.name}'s work: say you had "
                f"asked {assignee.name} and what they reported, in your own words. Do not start "
                "new work unless the result requires it.")

    # ------------------------------------------------------------ agents

    def build_agent(self, agent: AgentDef, *, as_worker: bool, on_fallback: bool = False,
                    memory_context: str = "", skills_context: str = "",
                    knowledge_context: str = "", window: int | None = None,
                    thread_id: str | None = None,
                    model_settings: ModelSettings | None = None) -> Agent:
        instructions = [agent.instructions or f"You are {agent.name}."]
        about_user = profile.context_for(agent)
        if about_user:
            instructions.append(about_user)
        roster = team_roster(agent)
        if roster:
            instructions.append(roster)
        if as_worker:
            instructions.append(
                "You are working on a task delegated by another agent. Do the work, then "
                "return a WorkResult. If you cannot finish, return status 'failed' or "
                "'partial' and explain why in the summary.")
        toolsets = build_tool_toolsets(agent, wrap=self._with_approvals)
        mcp_count = len(toolsets)  # the first ones are your tool servers; the rest are Agent HQ's
        if toolsets:
            instructions.append(APPROVAL_NOTE)
        if any(desk for _, desk in toolsets):
            instructions.append(DESKTOP_RULES)
            toolsets.append((self._handoff_toolset(), None))
        if has_vision(agent):
            # The main model never gets screenshots: hide every screenshot tool on desktop
            # servers and give it look / locate / click_on instead.
            toolsets = [(ts.filtered(lambda ctx, td: not td.name.endswith("screenshot")) if desk else ts, desk)
                        for ts, desk in toolsets]
            instructions.append(VISION_RULES)
            toolsets.append((self._vision_toolset(), None))
        elif any(desk for _, desk in toolsets):
            instructions.append(NO_VISION_RULES)
        if has_fallback(agent) and not on_fallback:
            instructions.append(ESCALATE_RULES)
            toolsets.append((self._escalate_toolset(), None))
        if memory_on(agent):
            instructions.append(MEMORY_RULES)
            if memory_context:
                instructions.append(memory_context)
            toolsets.append((self._memory_toolset(), None))
        if roster:
            toolsets.append((self._delegation_toolset(), None))
        if knowledge_context or knowledge.has_knowledge(agent):
            instructions.append(knowledge.KNOWLEDGE_RULES)
            if knowledge_context:
                instructions.append(knowledge_context)
            toolsets.append((self._knowledge_toolset(), None))
        learning = skills.mode_of(agent)
        if skills_context or learning != LearningMode.off:
            instructions.append(SKILL_INTRO + (("\n" + skills.PROPOSE_RULES)
                                               if learning != LearningMode.off else ""))
            if skills_context:
                instructions.append(skills_context)
            toolsets.append((self._skills_toolset(learning), None))
        if learning != LearningMode.off and kprop.enabled():
            instructions.append(kprop.SUGGEST_RULES)
            toolsets.append((self._knowledge_suggest_toolset(), None))
        files_here = bool(thread_id) and att.has_any(thread_id)
        workbench = any(not desk for _, desk in toolsets[:mcp_count])
        on_desktop = any(desk for _, desk in toolsets[:mcp_count])
        instructions.append(att.rules(files_here, workbench, on_desktop))
        instructions.append(WORK_RULES)
        toolsets.append((self._attachments_toolset(files_here, workbench, on_desktop), None))
        mode = subagents.mode_of(agent)
        if mode != SubagentMode.off:
            instructions.append(subagent_rules(agent, mode))
            toolsets.append((self._subagent_toolset(mode), None))
        if not as_worker and thread_id:
            goal = workitems.goal_for_thread(thread_id)
            if goal is not None and goal.owner_agent_id == agent.id:
                instructions.append(workitems.GOAL_RULES)
                toolsets.append((workitems.goal_toolset(
                    self, team=workitems.team_size(agent) > 1,
                    sub_agents=mode != SubagentMode.off,
                    agents=bool(workitems.team_agents(agent))), None))
        if not agent.is_subagent:
            instructions.append(workitems.PROPOSE_RULES)
            toolsets.append((workitems.propose_toolset(self), None))
        if agent.is_subagent:
            instructions.append(SUBAGENT_SELF_RULES)
        instructions.append(clock.instructions())  # last: it changes every minute (caching)
        model = (build_model_for(agent.fallback_provider_id, agent.fallback_model,
                                 f"{agent.name} (fallback)")
                 if on_fallback else build_agent_model(agent))
        text = "\n\n".join(instructions)
        used = context.tokens(text)
        reserve = int(context.get_settings()["reserve_tokens"])
        # Within a run, older tool results are trimmed harder as it nears the window.
        trimmer = context.make_trimmer(int(window * 0.9) - used - reserve if window else 0)
        shared = resources.shared_desktops([desk for _, desk in toolsets if desk])
        built = Agent(
            model,
            name=agent.name,
            instructions=text,
            deps_type=RunDeps,
            # Delegated agents should hand in a WorkResult, but small models often just write
            # their answer as text; accept that too rather than throwing the work away.
            output_type=[WorkResult, str] if as_worker else str,
            retries=3,  # a malformed tool call or result gets a few tries, not one
            capabilities=[ProcessHistory(trimmer)],
            # The agent's thinking level, translated for its model (app/thinking.py).
            model_settings=model_settings,
            toolsets=[ObservedToolset(t, agent_name=agent.name, broker=self.broker, desktop_server_id=desk,
                                      lease_resource_id=shared.get(desk) if desk else None,
                                      source="mcp" if i < mcp_count else "internal")
                      for i, (t, desk) in enumerate(toolsets)],
        )
        built.ahq_instruction_tokens = used  # for fitting the history around them

        @built.output_validator
        def keep_working(ctx: RunContext[RunDeps], output):
            # Small models often narrate the next step and stop. Push back a couple of times
            # so the turn carries on instead of ending on a promise.
            if isinstance(output, str) and ctx.retry < MAX_CONTINUE_NUDGES:
                last = ctx.messages[-1] if ctx.messages else None
                if isinstance(last, ModelResponse) and last.finish_reason == "length":
                    raise ModelRetry(TRUNCATED_NUDGE)
                if announces_unstarted_work(output):
                    raise ModelRetry(CONTINUE_NUDGE)
                if links := dead_file_links(output):
                    raise ModelRetry(FILE_LINK_NUDGE.format(links=", ".join(links[:5])))
                if (files := untested_changes(ctx.messages)) and not _SAYS_UNTESTED.search(output):
                    raise ModelRetry(UNTESTED_NUDGE.format(files=", ".join(files[:5])))
            return output

        return built

    async def run_agent(self, agent: AgentDef, prompt: str, deps: RunDeps, *,
                        history: list | None = None, as_worker: bool = False):
        """Run the agent (see _run_agent), then let go of any shared desktop this run took."""
        try:
            return await self._run_agent(agent, prompt, deps, history=history, as_worker=as_worker)
        finally:
            if deps.replay is None:
                shared = resources.shared_desktops([srv.id for srv in desktop_servers(agent)])
                for res_id in set(shared.values()):
                    try:
                        await resources.release_lease(self.broker.r, res_id, deps.job_id)
                    except Exception as e:  # it expires by itself anyway
                        log.warning("couldn't release shared desktop %s: %s", res_id, e)

    async def _run_agent(self, agent: AgentDef, prompt: str, deps: RunDeps, *,
                         history: list | None = None, as_worker: bool = False):
        """Run on the agent's model; on failure or escalation, rerun on its fallback model.

        Every run is written to the usage ledger. Sub-agents run under their caps: a limit
        on model requests and a time limit.
        """
        recalled = await self._recall(agent, prompt, deps)
        excerpts = await self._knowledge_recall(agent, prompt, deps)
        try:
            skills_block = skills.listing(await skills.relevant(agent, prompt))
        except Exception as e:  # skills must never stop the agent from working
            log.warning("skill listing failed: %s", e)
            skills_block = ""
        servers = agent_tool_servers(agent)
        await self.broker.publish(deps.thread_id, "agent_tools", agent=agent.name, agent_id=agent.id,
                                  servers=[s.name for s in servers], depth=deps.depth,
                                  subagent=agent.is_subagent)
        run_kw: dict[str, Any] = {}
        minutes = None
        if agent.is_subagent:
            caps = agent.subagent_limits or {}
            run_kw["usage_limits"] = UsageLimits(request_limit=int(caps.get("max_requests") or 25))
            minutes = float(caps.get("max_minutes") or 10)

        progress: list = []  # what the main model did before it failed, for the fallback

        async def attempt(on_fallback: bool, text: str | None, resume: list | None = None,
                          think: str | None = None):
            nonlocal history
            pid, model = ((agent.fallback_provider_id, agent.fallback_model) if on_fallback
                          else (agent.provider_id, agent.model))
            started = asyncio.get_running_loop().time()
            usage, ok = None, False
            try:
                window, _ = await context.window_for(agent, on_fallback=on_fallback)
                pa_agent = self.build_agent(agent, as_worker=as_worker, on_fallback=on_fallback,
                                            memory_context=recalled, skills_context=skills_block,
                                            knowledge_context=excerpts, window=window,
                                            thread_id=deps.thread_id,
                                            model_settings=(ModelSettings(**{
                                                **(await thinking.settings_for(
                                                    agent, on_fallback=on_fallback, override=think) or {}),
                                                **limit_settings(agent, on_fallback=on_fallback)}) or None))
                start = resume if resume is not None else history
                start = await self._fit_history(agent, pa_agent, start, window, text or "", deps)
                if resume is None:
                    history = start
                coro = pa_agent.run(self._user_input(agent, text, deps) if text is not None else None,
                                    deps=deps, message_history=start,
                                    conversation_id=deps.thread_id, **run_kw)
                with capture_run_messages() as captured:
                    try:
                        result = await (asyncio.wait_for(coro, minutes * 60) if minutes else coro)
                    except asyncio.TimeoutError:
                        progress[:] = captured
                        raise RuntimeError(f"stopped at its {minutes:g}-minute limit") from None
                    except Exception:
                        progress[:] = captured
                        raise
                usage, ok = result.usage, True
                # Images were for this run; the saved history keeps a placeholder instead.
                return result.output, att.strip_images(result.all_messages())
            finally:
                subagents.record_usage(agent, provider_id=pid, model=model, usage=usage,
                                       seconds=asyncio.get_running_loop().time() - started, ok=ok,
                                       job_id=deps.job_id, thread_id=deps.thread_id)

        def resume_from(note: str) -> tuple[str | None, list | None]:
            """Carry on from the failed run's last complete step, with a note to the model."""
            resume = _resume_point(att.strip_images(progress))
            if resume and isinstance(resume[-1], ModelRequest):
                last = resume[-1]
                resume[-1] = dataclasses.replace(last, parts=[*last.parts, UserPromptPart(note)])
                return None, resume
            return f"{prompt}\n\n{note}", None

        try:
            try:
                return await attempt(False, prompt)
            except asyncio.CancelledError:
                raise
            except Exception as first:
                # A local model that thought itself past its output or time limit usually
                # does fine with less thinking: try that once before paying for the fallback.
                why = ran_out(first)
                if why is None or (agent.thinking or "").lower() in ("off", "minimal", "low"):
                    raise
                await self.save_message(
                    deps.thread_id, Role.event,
                    f"{agent.name} stopped because {why}; trying again on the same model with "
                    f"less thinking.", agent_id=agent.id, kind="think_less")
                text, resume = resume_from(
                    f"[Your last attempt stopped because {why}. Think briefly and act: take the "
                    "next step with a tool call.]")
                return await attempt(False, text, resume, think="less")
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if not isinstance(e, Exception):
                raise
            # A tool server that is down: report it by name, and don't try the fallback
            # model, which can't fix it.
            down = _find_exc(e, ToolServerUnreachable)
            if down is not None and not _find_exc(e, Escalation):
                raise down
            if not has_fallback(agent):
                raise
            esc = _find_exc(e, Escalation)
            reason = f"escalated: {esc.reason}" if esc else f"{type(e).__name__}: {e}"
            target = describe_model(agent.fallback_provider_id, agent.fallback_model)
            await self.broker.publish(deps.thread_id, "model_fallback", agent=agent.name,
                                      agent_id=agent.id, to_model=target, reason=reason[:300],
                                      depth=deps.depth)
            await self.save_message(deps.thread_id, Role.event,
                                    f"{agent.name} switched to its fallback model {target} ({reason[:300]})",
                                    agent_id=agent.id, kind="model_fallback")
            note = (f"[A smaller model attempted this first and stopped: {reason[:500]}. "
                    "Its work so far is above, and any files it wrote are still there. "
                    "Take over from where it stopped and finish the task yourself with your "
                    "tools. Its work may be incomplete or wrong: fix or redo what you need to. "
                    "Don't stop just to report what it got wrong; stop only when the task is "
                    "done, you are blocked, or you need the user to decide something.]")
            text, resume = resume_from(note)
            return await attempt(True, text, resume)

    @staticmethod
    def _user_input(agent: AgentDef, text: str, deps: RunDeps):
        """The prompt, with the attached images when the agent's own model sees images."""
        if not (agent.sees_images and deps.attachment_ids):
            return text
        try:
            images = [att.model_image(a) for a in att.in_thread(deps.thread_id, deps.attachment_ids)
                      if a.kind == "image"]
        except Exception as e:  # a bad image must not stop the turn; the tools still work
            log.warning("attachments: images not sent: %s", e)
            return text
        return [text, *images] if images else text

    async def _fit_history(self, agent: AgentDef, pa_agent: Agent, history: list | None,
                           window: int, prompt: str, deps: RunDeps) -> list | None:
        """Compact the conversation so instructions + history + the new message fit the
        window; warn once a day if the instructions alone leave too little room."""
        cfg = context.get_settings()
        used = getattr(pa_agent, "ahq_instruction_tokens", 0) + context.tokens(prompt)
        reserve = int(cfg["reserve_tokens"])
        if cfg["warn_overflow"] and used + reserve > window * 0.9 and deps.replay is None:
            try:
                if await self.broker.r.set(f"ahq:ctxwarn:{deps.thread_id}:{agent.id}", "1", nx=True, ex=86400):
                    await self.save_message(
                        deps.thread_id, Role.event,
                        f"{agent.name}'s instructions and this message take about {used:,} tokens, but its "
                        f"context window is {window:,}, so the model can't see all of it. Raise the window "
                        f"(for Ollama: OLLAMA_CONTEXT_LENGTH on the server, or a Modelfile with num_ctx) and "
                        f"set it on the agent, or trim its instructions, tools or recall settings.",
                        agent_id=agent.id, kind="context_warning", window=window, used=used)
            except Exception as e:
                log.warning("context warning failed: %s", e)
        if not history:
            return history
        budget = max(512, int(window * float(cfg["trigger_ratio"])) - used - reserve)
        try:
            fitted, done = await context.compact(agent, history, budget=budget, window=window)
        except Exception as e:  # never let this stop the agent from answering
            log.warning("compaction failed for %s: %s", agent.name, e)
            return history
        if done is not None and deps.replay is None:
            what = ("summarised" if done.method == "summary" else "removed (a summary couldn't be written)")
            await self.save_message(
                deps.thread_id, Role.event,
                f"{agent.name}'s earlier conversation ({done.turns} turn{'s' if done.turns != 1 else ''}) was "
                f"{what} to fit its {window:,}-token context window: about {done.before:,} → {done.after:,} tokens.",
                agent_id=agent.id, kind="compaction", summary=done.summary, method=done.method,
                turns=done.turns, model=done.model)
            await self.broker.publish(deps.thread_id, "compaction", agent=agent.name, turns=done.turns,
                                      before=done.before, after=done.after, depth=deps.depth)
        return fitted

    # ------------------------------------------------------------ memory

    async def _recall(self, agent: AgentDef, prompt: str, deps: RunDeps) -> str:
        if not memory_on(agent):
            return ""
        try:
            block = await memory.recall_for(agent, prompt, deps.thread_id)
        except Exception as e:  # memory must never stop the agent from working
            log.warning("memory recall failed: %s", e)
            return ""
        if block:
            await self.broker.publish(deps.thread_id, "memory_recall", agent=agent.name,
                                      agent_id=agent.id, count=block.count("\n- "), depth=deps.depth)
        return block

    async def _knowledge_recall(self, agent: AgentDef, prompt: str, deps: RunDeps) -> str:
        try:
            block = await knowledge.recall_for(agent, prompt)
        except Exception as e:  # the library must never stop the agent from working
            log.warning("knowledge recall failed: %s", e)
            return ""
        if block:
            await self.broker.publish(deps.thread_id, "knowledge_recall", agent=agent.name,
                                      agent_id=agent.id, count=block.count("\n["), depth=deps.depth)
        return block

    def _knowledge_toolset(self) -> FunctionToolset:
        rt = self

        async def search_knowledge(ctx: RunContext[RunDeps], query: str, collection: str | None = None,
                                   tags: list[str] | None = None, limit: int = 5) -> dict:
            """Search the user's documents and notes (the knowledge library).

            Args:
                query: what you're looking for, in plain words.
                collection: only this collection (by name); omit to search all you can read.
                tags: only documents with one of these tags (e.g. ["homelab"]).
                limit: how many excerpts (1-10).
            """
            me = load_agent(ctx.deps.agent_id)
            cols = knowledge.readable_collections(me)
            if collection:
                cols = [c for c in cols if c.name.lower() == collection.strip().lower()] or cols
            hits = await knowledge.search(query, collection_ids=[c.id for c in cols],
                                          k=max(1, min(int(limit or 5), 10)), tags=tags)
            await rt.broker.publish(ctx.deps.thread_id, "knowledge_search", agent=me.name, query=query,
                                    count=len(hits), depth=ctx.deps.depth)
            return {"results": [h.out() for h in hits],
                    "collections": [c.name for c in knowledge.readable_collections(me)]}

        async def read_document(ctx: RunContext[RunDeps], ref: str, part: int = 1) -> dict:
            """Read a whole document from the knowledge library.

            Args:
                ref: the doc id from a search result or excerpt, or its path or title.
                part: long documents come in parts of about 20,000 characters; 1 is the first.
            """
            me = load_agent(ctx.deps.agent_id)
            d = knowledge.find_doc(ref, me)
            if d is None:
                return {"error": f"no document {ref!r} that you can read; try search_knowledge"}
            size = 20000
            parts = max(1, -(-len(d.text) // size))
            n = max(1, min(int(part or 1), parts))
            return {"doc_id": d.id, "title": d.title, "path": d.path, "link": d.link, "tags": d.tags,
                    "links": d.links[:50], "part": n, "parts": parts,
                    "text": d.text[(n - 1) * size:n * size]}

        return FunctionToolset([search_knowledge, read_document])

    async def _capture_delegation(self, job: Job, assignee: AgentDef, result: WorkResult) -> None:
        if not memory.get_settings()["capture_delegations"] or not memory_on(assignee):
            return
        # Only completed work. Failures are usually setup problems or one-offs ("I can't
        # browse" when a tool server was missing); remembering them teaches agents to give up.
        if result.status != "done":
            return
        text = (f"{assignee.name} was asked: {job.task[:400]}\n"
                f"Outcome ({result.status}): {result.summary[:800]}")
        if result.details:
            text += f"\nDetails: {result.details[:600]}"
        try:
            await memory.capture(text, kind="incident", scope="team", thread_id=job.origin_thread_id,
                                 source="auto:delegation", source_job_id=job.id,
                                 created_by=assignee.id)
        except Exception as e:
            log.warning("memory capture failed: %s", e)

    async def _capture_event(self, job: Job, reply: str) -> None:
        owner = load_agent(job.assignee_agent_id)
        if not memory.get_settings()["capture_events"] or not memory_on(owner):
            return
        meta = job.meta or {}
        via = "webhook event" if meta.get("kind") == "webhook" else "MQTT topic"
        text = (f"Event on {via} {meta.get('topic', '?')}: {str(meta.get('event', ''))[:400]}\n"
                f"{owner.name}'s assessment: {reply[:800]}")
        try:
            await memory.capture(text, kind="incident", scope="team", thread_id=job.origin_thread_id,
                                 source="auto:event", source_job_id=job.id, created_by=owner.id)
        except Exception as e:
            log.warning("memory capture failed: %s", e)

    def _memory_toolset(self) -> FunctionToolset:
        async def remember(ctx: RunContext[RunDeps], text: str, kind: str = "fact",
                           scope: str = "team", supersedes: str | None = None) -> dict:
            """Save something worth knowing next time.

            Args:
                text: One self-contained statement, e.g. "pve1's backups run nightly at 02:00 to PBS on rewind".
                kind: fact, incident, preference, procedure or note.
                scope: "team" (all agents) or "agent" (only you).
                supersedes: ID of a memory this replaces; the old one is retired.
            """
            if kind not in MemoryKind.__members__:
                kind = "note"
            scope = "agent" if scope == "agent" else "team"
            m = await memory.add(text, kind=kind, scope=scope,
                                 agent_id=ctx.deps.agent_id if scope == "agent" else None,
                                 thread_id=ctx.deps.thread_id, source="agent",
                                 source_job_id=ctx.deps.job_id, created_by=ctx.deps.agent_id,
                                 supersedes=supersedes)
            return {"saved": m.id}

        async def recall(ctx: RunContext[RunDeps], query: str, include_retired: bool = False) -> list[dict]:
            """Search long-term memory.

            Args:
                query: What you want to know, e.g. "pve1 disk" or "how do we patch OPNsense".
                include_retired: also show memories that were retired as outdated.
            """
            hits = await memory.search(query, agent_id=ctx.deps.agent_id, thread_id=ctx.deps.thread_id,
                                       k=8, include_retired=include_retired, min_score=0.1)
            return [{"id": h.memory.id, "text": h.memory.text, "kind": h.memory.kind.value,
                     "date": h.memory.last_confirmed.strftime("%Y-%m-%d"),
                     "retired": h.memory.invalidated_reason} for h in hits]

        async def forget(ctx: RunContext[RunDeps], memory_id: str, reason: str) -> dict:
            """Retire a memory that is wrong or no longer true (it is kept as history).

            Args:
                memory_id: The ID shown in brackets.
                reason: Why, e.g. "disk was cleaned up on 2026-11-06".
            """
            try:
                memory.retire(memory_id, reason)
                return {"retired": memory_id}
            except LookupError:
                return {"error": "no such memory"}

        async def confirm(ctx: RunContext[RunDeps], memory_id: str) -> dict:
            """Mark a memory as verified just now, so it ranks as current."""
            try:
                memory.confirm(memory_id)
                return {"confirmed": memory_id}
            except LookupError:
                return {"error": "no such memory"}

        return FunctionToolset([remember, recall, forget, confirm], id="memory")

    # ------------------------------------------------------------ vision and escalation tools

    def _vision_toolset(self) -> FunctionToolset:
        from .vision import DesktopEyes, screenshot_server

        rt = self

        async def eyes(ctx: RunContext[RunDeps]):
            me = load_agent(ctx.deps.agent_id)
            servers = desktop_servers(me)
            srv = await screenshot_server(me, servers)
            if srv is None:
                return None, "No desktop with a screenshot tool is reachable."
            if await rt.broker.user_has_control(srv.id):
                await rt.broker.mark_blocked(srv.id, {"agent_id": me.id, "thread_id": ctx.deps.thread_id})
                return None, DESKTOP_PAUSED
            return DesktopEyes(me, srv, ctx.deps.thread_id), None

        async def look(ctx: RunContext[RunDeps], question: str) -> str:
            """Ask a question about what is currently on the screen.

            Args:
                question: e.g. "What is the highest CPU value on the graph, and when?"
            """
            e, err = await eyes(ctx)
            return err if e is None else await e.look(question)

        async def locate(ctx: RunContext[RunDeps], description: str) -> dict:
            """Find something on the screen by description. Returns its x/y click coordinates.

            Args:
                description: e.g. "the blue Sign in button under the password field"
            """
            e, err = await eyes(ctx)
            return {"found": False, "error": err} if e is None else await e.locate(description)

        async def click_on(ctx: RunContext[RunDeps], description: str, double: bool = False) -> dict:
            """Find something on the screen by description and click it.

            Args:
                description: what to click, described as you would to a person.
                double: double-click instead of a single click.
            """
            e, err = await eyes(ctx)
            if e is None:
                return {"clicked": False, "error": err}
            spot = await e.locate(description)
            if not spot.get("found"):
                return {"clicked": False, "error": f"could not find: {description}"}
            msg = await e.click(spot["x"], spot["y"], double)
            return {"clicked": True, "x": spot["x"], "y": spot["y"], "result": msg}

        return FunctionToolset([look, locate, click_on], id="vision")

    def _attachments_toolset(self, files_here: bool, workbench: bool, desktop: bool = False) -> FunctionToolset:
        """Reading the conversation's files, and sending files back to the user."""

        def files(ctx: RunContext[RunDeps], ids: list[str]):
            return att.in_thread(ctx.deps.thread_id, ids)

        def made_count(ctx: RunContext[RunDeps]) -> int:
            return len(att.made_by_job(ctx.deps.job_id))

        def rehearsal(ctx: RunContext[RunDeps], name: str) -> dict | None:
            # An eval run has no real conversation to put files in: say it worked.
            if ctx.deps.replay is None:
                return None
            return {"attached": True, "attachment": "att_000000000000", "name": name,
                    "note": "(test run: nothing was stored)"}

        async def list_attachments(ctx: RunContext[RunDeps]) -> list[dict]:
            """Every file in this conversation: the user's and the ones agents attached."""
            out = []
            for a in att.for_thread(ctx.deps.thread_id):
                who = "user" if a.source == "user" else (load_agent(a.agent_id).name if a.agent_id else "agent")
                out.append({"attachment": att.ref(a.id), "name": a.name, "kind": a.kind,
                            "size": att.human_size(a.size), "from": who,
                            "readable_text": a.text is not None})
            return out

        async def read_attachment(ctx: RunContext[RunDeps], attachment_id: str, offset: int = 0,
                                  max_chars: int = att.READ_CHARS) -> dict:
            """Read the text of an attached document or text file, a page at a time.

            Args:
                attachment_id: e.g. att_ab12cd34ef56
                offset: where to start; use next_offset from the previous call to continue.
                max_chars: how much to read (up to 30000).
            """
            try:
                (a,) = files(ctx, [attachment_id])
            except att.AttachmentError as e:
                return {"error": str(e)}
            return att.read_text(a, offset, max_chars)

        async def look_at_attachment(ctx: RunContext[RunDeps], attachment_id: str, question: str):
            """Ask a question about an attached image (photo, screenshot, chart, scan).

            Args:
                attachment_id: e.g. att_ab12cd34ef56
                question: what you want to know, e.g. "What error message is shown?"
            """
            from .vision import ask_image

            try:
                (a,) = files(ctx, [attachment_id])
            except att.AttachmentError as e:
                return str(e)
            if a.kind != "image":
                return f"{a.name} is not an image; read it with read_attachment."
            me = load_agent(ctx.deps.agent_id)
            if me.vision_model:
                return await ask_image(me, question, att.image_png(a), ctx.deps.thread_id)
            if me.sees_images:
                return ToolReturn(return_value=f"Here is {a.name}; look at it to answer: {question}",
                                  content=[att.model_image(a)])
            return (f"You have no way to see images: no vision model is set for you. Tell the "
                    f"user you can't view {a.name}, and that they can set a vision model (or "
                    "turn on 'Model sees images' for a multimodal model) under Settings → Agents.")

        async def attachment_to_workbench(ctx: RunContext[RunDeps], attachment_id: str,
                                          path: str = "") -> dict:
            """Copy a file from this conversation into your workbench, to work on it with code.

            Args:
                attachment_id: e.g. att_ab12cd34ef56
                path: where to put it in your workspace (default: inbox/<file name>).
            """
            try:
                (a,) = files(ctx, [attachment_id])
            except att.AttachmentError as e:
                return {"error": str(e)}
            srv = await att.workbench_server(load_agent(ctx.deps.agent_id))
            if srv is None:
                return {"error": "you have no workbench that can receive files (or it is not reachable)"}
            try:
                r = await att.to_workbench(srv, a, path or f"inbox/{a.name}")
            except Exception as e:
                return {"error": f"copy failed: {e}"}
            return {"copied": True, **r}

        async def create_file(ctx: RunContext[RunDeps], name: str, content: str) -> dict:
            """Give the user a file you wrote: a report, CSV, JSON, config, script, Markdown...

            Args:
                name: the file name with its extension, e.g. "ups-report.md" or "hosts.csv".
                content: the full text of the file.
            """
            if (fake := rehearsal(ctx, name)) is not None:
                return fake
            if made_count(ctx) >= MAX_FILES_PER_JOB:
                return {"error": f"you can attach at most {MAX_FILES_PER_JOB} files per task"}
            try:
                a = att.store(ctx.deps.thread_id, name, content.encode("utf-8"), source="agent",
                              agent_id=ctx.deps.agent_id, job_id=ctx.deps.job_id)
            except att.AttachmentError as e:
                return {"error": str(e)}
            return att.Outgoing(a).result()

        async def send_back(ctx: RunContext[RunDeps], where: str, path: str, name: str) -> dict:
            if (fake := rehearsal(ctx, name or path)) is not None:
                return fake
            if made_count(ctx) >= MAX_FILES_PER_JOB:
                return {"error": f"you can attach at most {MAX_FILES_PER_JOB} files per task"}
            me = load_agent(ctx.deps.agent_id)
            srv = await (att.desktop_server(me) if where == "desktop" else att.workbench_server(me))
            if srv is None:
                return {"error": f"you have no {where} that can send files (or it is not reachable)"}
            try:
                fname, data = await att.fetch_file(srv, path)
                # off the event loop: a scanned PDF is OCR'd while it's stored
                a = await asyncio.get_running_loop().run_in_executor(None, lambda: att.store(
                    ctx.deps.thread_id, name or fname, data, source=where,
                    agent_id=ctx.deps.agent_id, job_id=ctx.deps.job_id))
            except Exception as e:
                return {"error": str(e)}
            return att.Outgoing(a).result()

        async def send_from_workbench(ctx: RunContext[RunDeps], path: str, name: str = "") -> dict:
            """Give the user a file from your workbench (a chart, a spreadsheet, an export...).

            Args:
                path: the file's path in your workspace, e.g. "out/chart.png".
                name: the name to show the user (default: the file's own name).
            """
            return await send_back(ctx, "workbench", path, name)

        async def send_from_desktop(ctx: RunContext[RunDeps], path: str, name: str = "") -> dict:
            """Give the user a file from your desktop, e.g. one the browser downloaded.

            Args:
                path: the file's path, e.g. "~/Downloads/invoice.pdf" (see list_desktop_files).
                name: the name to show the user (default: the file's own name).
            """
            return await send_back(ctx, "desktop", path, name)

        async def list_desktop_files(ctx: RunContext[RunDeps], folder: str = "") -> dict:
            """The newest files your desktop downloaded (~/Downloads and the browser's download folder).

            Args:
                folder: a folder to list instead, e.g. "~/Documents" or "~/Uploads".
            """
            srv = await att.desktop_server(load_agent(ctx.deps.agent_id))
            if srv is None:
                return {"error": "your desktop can't list files (or it is not reachable)"}
            try:
                return {"files": await att.list_desktop(srv, folder)}
            except Exception as e:
                return {"error": str(e)}

        async def attachment_to_desktop(ctx: RunContext[RunDeps], attachment_id: str) -> dict:
            """Copy a file from this conversation onto your desktop (into ~/Uploads), e.g. to
            upload it to a website. Returns its full path there.

            Args:
                attachment_id: e.g. att_ab12cd34ef56
            """
            try:
                (a,) = files(ctx, [attachment_id])
            except att.AttachmentError as e:
                return {"error": str(e)}
            srv = await att.desktop_server(load_agent(ctx.deps.agent_id))
            if srv is None:
                return {"error": "your desktop can't receive files (or it is not reachable)"}
            try:
                r = await att.to_desktop(srv, a)
            except Exception as e:
                return {"error": f"copy failed: {e}"}
            return {"copied": True, **r,
                    "next": "Click the page's upload button or file field, then call browser_file_upload "
                            "with this path when the file chooser is waiting."}

        tools = [create_file]
        if files_here:
            tools = [list_attachments, read_attachment, look_at_attachment, *tools]
        if workbench:
            tools.append(send_from_workbench)
            if files_here:
                tools.append(attachment_to_workbench)
        if desktop:
            tools += [list_desktop_files, send_from_desktop]
            if files_here:
                tools.append(attachment_to_desktop)
        return FunctionToolset(tools, id="attachments")

    def _escalate_toolset(self) -> FunctionToolset:
        async def escalate(ctx: RunContext[RunDeps], reason: str) -> str:
            """Hand this task to a stronger model when you cannot do it well.

            Args:
                reason: what you tried and why you are stuck.
            """
            raise Escalation(reason)

        return FunctionToolset([escalate], id="escalate")

    # ------------------------------------------------------------ desktop handoff tool

    def _handoff_toolset(self) -> FunctionToolset:
        rt = self

        async def ask_user_to_take_over(ctx: RunContext[RunDeps], reason: str) -> dict:
            """Ask the user to take control of your desktop (to log in, solve a CAPTCHA,
            enter an MFA code...) and wait until they hand it back.

            Args:
                reason: What the user needs to do, e.g. "Log in to the Zabbix web UI".
            """
            me = load_agent(ctx.deps.agent_id)
            servers = desktop_servers(me)
            if not servers:
                return {"status": "failed", "summary": "You have no desktop."}
            srv = servers[0]
            timeout = settings.handoff_timeout
            await rt.broker.set_request(srv.id, {
                "agent_id": me.id, "agent": me.name, "thread_id": ctx.deps.thread_id,
                "job_id": ctx.deps.job_id, "reason": reason}, ttl=int(timeout) + 60)
            await rt.save_message(ctx.deps.thread_id, Role.event,
                                  f"{me.name} needs you on its desktop: {reason}",
                                  agent_id=me.id, kind="desktop_request", tool_server_id=srv.id)
            workitems.mark_waiting(ctx.deps.thread_id, "handoff", f"{me.name} needs you on its desktop: {reason}")
            try:
                async with released_slot():
                    got = await rt.broker.wait_handback(srv.id, timeout)
            finally:
                workitems.clear_waiting(ctx.deps.thread_id, "handoff")
            if got is None:
                await rt.broker.clear_request(srv.id)
                await rt.save_message(ctx.deps.thread_id, Role.event,
                                      f"{me.name} stopped waiting for you after {int(timeout)}s.",
                                      agent_id=me.id, kind="desktop_timeout")
                return {"status": "no_response",
                        "summary": "The user did not take over in time. Report that the step "
                                   "still needs them."}
            return {"status": "handed_back", "note": got.get("note") or "(no note)",
                    "summary": "The user has handed the desktop back. Take a screenshot and continue."}

        return FunctionToolset([ask_user_to_take_over], id="handoff")

    # ------------------------------------------------------------ delegation tools

    def _delegation_toolset(self) -> FunctionToolset:
        rt = self

        async def delegate(ctx: RunContext[RunDeps], agent_name: str, task: str,
                           background: bool = False, attachment_ids: list[str] | None = None) -> dict:
            """Hand a task to a team member and get their result.

            Args:
                agent_name: Name of the team member (from the team list in your instructions).
                task: A complete, self-contained description of what to do and what to return.
                background: False (default) waits and returns their result now. True returns a
                    job ID immediately; you will be woken in this conversation with the result.
                attachment_ids: Files from this conversation they need, e.g. ["att_ab12cd34ef56"].
            """
            me = load_agent(ctx.deps.agent_id)
            target = find_delegate(me, agent_name)
            if target is None:
                names = ", ".join(a.name for a in allowed_delegates(me)) or "nobody"
                return {"status": "failed", "summary": f"'{agent_name}' is not on your team. "
                        f"You can delegate to: {names}."}
            if target.id in ctx.deps.chain:
                return {"status": "failed", "summary": f"{target.name} is already working on "
                        "this chain; delegating back would loop. Do it yourself or ask the user."}
            if ctx.deps.depth + 1 > settings.max_delegation_depth:
                return {"status": "failed", "summary": "Delegation chain is too deep."}
            try:
                given = att.in_thread(ctx.deps.thread_id, attachment_ids or [])
            except att.AttachmentError as e:
                return {"status": "failed", "summary": f"{e}. See list_attachments for the IDs."}
            full_task = task + ("\n\n" + att.manifest(given, sees_images=target.sees_images,
                                                      heading="Files for this task") if given else "")

            job = Job(
                kind=JobKind.task, origin_thread_id=ctx.deps.thread_id, requester_agent_id=me.id,
                assignee_agent_id=target.id, parent_job_id=ctx.deps.job_id, task=full_task,
                background=background, depth=ctx.deps.depth + 1,
                chain=[*ctx.deps.chain, target.id], pool=target.pool,
                meta={**({"attachments": [a.id for a in given]} if given else {}),
                      **({"cautious": True} if ctx.deps.cautious else {})})
            # Show the exchange in the conversation right away; it's updated in place as the
            # task starts and finishes. Created before queueing, so a fast worker can't win.
            msg = await rt.save_message(
                ctx.deps.thread_id, Role.event, f"{me.name} → {target.name}: {task}",
                agent_id=target.id, kind="delegation_result", job_id=job.id, status="queued",
                task=task, requester_id=me.id, requester=me.name, assignee=target.name,
                background=background, depth=job.depth, started_at=_now().isoformat(),
                **({"given": [att.summary(a) for a in given]} if given else {}))
            job.meta = {**(job.meta or {}), "message_id": msg.id}
            job = await rt._submit(job)
            if background:
                return {"status": "queued", "job_id": job.id,
                        "summary": f"{target.name} is working on it in the background. You will "
                        "be woken in this conversation with the result. Tell the user it is underway."}

            async with released_slot():
                result = await rt.broker.wait_result(job.id, settings.delegate_wait_timeout)
            if result is not None:
                return attributed(target.name, result, target.pronouns)
            # Taking too long: convert to a background job so the result still gets reported.
            job = update_job(job.id, background=True)
            if job.status in (JobStatus.done, JobStatus.failed) and job.result:
                return attributed(target.name, job.result, target.pronouns)
            return {"status": "queued", "job_id": job.id,
                    "summary": f"{target.name} is still working after "
                    f"{int(settings.delegate_wait_timeout)}s. It now runs in the background; you "
                    "will be woken in this conversation with the result. Tell the user."}

        async def job_status(ctx: RunContext[RunDeps], job_id: str) -> dict:
            """Check on a background job started in this conversation."""
            job = load_job(job_id)
            if job is None or job.origin_thread_id != ctx.deps.thread_id:
                return {"error": "no such job in this conversation"}
            return {"status": job.status, "attempts": job.attempts, "worker": job.worker,
                    "result": job.result, "error": job.error}

        return FunctionToolset([delegate, job_status], id="delegation")

    # ------------------------------------------------------------ skills

    def _skills_toolset(self, learning: LearningMode) -> FunctionToolset:
        rt = self

        async def load_skill(ctx: RunContext[RunDeps], name: str) -> dict:
            """Load a skill's full procedure by name, then follow it."""
            me = load_agent(ctx.deps.agent_id)
            k = skills.find(name, me)
            if k is None:
                return {"error": f"no skill named {name!r}; see list_skills"}
            if ctx.deps.replay is None:  # test runs don't count as uses
                skills.record_use(k, me.id, ctx.deps.job_id, ctx.deps.thread_id)
            await rt.broker.publish(ctx.deps.thread_id, "skill_loaded", agent=me.name, skill=k.name,
                                    version=k.version, depth=ctx.deps.depth)
            out = {"name": k.name, "title": k.title, "version": k.version,
                   "when_to_use": k.when_to_use, "procedure": k.body}
            if k.tools:
                out["tools"] = k.tools
            if k.script:
                out["workbench_script"] = (f"{k.script} (run it with your workbench's run_script)")
            return out

        def list_skills(ctx: RunContext[RunDeps], query: str = "") -> list[dict]:
            """List the skills you can use, optionally only those matching words in `query`."""
            me = load_agent(ctx.deps.agent_id)
            words = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())}
            out = []
            for k in skills.visible_to(me):
                hay = f"{k.name} {k.title} {k.when_to_use}".lower()
                if not words or any(w in hay for w in words):
                    out.append({"name": k.name, "title": k.title, "when_to_use": k.when_to_use[:300]})
            return out[:50]

        async def propose_skill(ctx: RunContext[RunDeps], name: str, title: str, when_to_use: str,
                                steps: str, reason: str, revise: str | None = None,
                                tools: list[str] | None = None, script: str | None = None,
                                only_for_me: bool = False) -> dict:
            """Propose a new skill, or an improvement to one, for the user to review.

            Args:
                name: lowercase_with_underscores, e.g. check_pve_backups (for a revision: the skill's name).
                title: a short title.
                when_to_use: one or two sentences on the situations it's for.
                steps: the procedure in markdown: the steps, what to check, the pitfalls.
                reason: what you learned that makes this worth keeping.
                revise: the name of an existing skill this improves (omit for a new skill).
                tools: tool names the procedure relies on.
                script: the name of a saved workbench script it uses, if any.
                only_for_me: true for a skill only you should see (default: the whole team).
            """
            me = load_agent(ctx.deps.agent_id)
            return await rt.submit_skill_proposal(
                me, name=name, title=title, when_to_use=when_to_use, steps=steps, reason=reason,
                revise=revise, tools=tools, script=script, only_for_me=only_for_me,
                thread_id=ctx.deps.thread_id, job_id=ctx.deps.job_id, depth=ctx.deps.depth)

        tools = [load_skill, list_skills] + ([propose_skill] if learning != LearningMode.off else [])
        return FunctionToolset(tools, id="skills")

    async def submit_skill_proposal(self, me: AgentDef, *, name: str, title: str, when_to_use: str,
                                    steps: str, reason: str, revise: str | None = None,
                                    tools: list[str] | None = None, script: str | None = None,
                                    only_for_me: bool = False, thread_id: str | None,
                                    job_id: str | None = None, depth: int = 0,
                                    source: str = "agent") -> dict:
        """A new skill or an improvement, from an agent's propose_skill or a reflection:
        sent for your review, or applied at once in auto mode."""
        via_note = {"reflection": " (from looking back at this conversation)",
                    "document": " (from a document in the knowledge library)"}.get(source, "")
        mode = skills.mode_of(me)
        if mode == LearningMode.off:
            return {"status": "refused", "summary": "You can't propose skills."}
        base = None
        if revise:
            base = skills.find(revise, me)
            if base is None:
                return {"status": "refused", "summary": f"There's no skill named {revise!r} to revise."}
            name = base.name
        elif skills.find(name) is not None:
            return {"status": "refused", "summary": f"A skill named {name} exists; pass revise={name!r} "
                    "to improve it, or choose another name."}
        why = skills.check_fields(name, title, when_to_use, steps)
        if why:
            return {"status": "refused", "summary": f"Not proposed: {why}."}
        with db.session() as s:
            open_ = s.exec(select(SkillProposal).where(SkillProposal.agent_id == me.id,
                                                       SkillProposal.status == "pending")).all()
        if any(p.name == name for p in open_):
            return {"status": "refused", "summary": f"You already have a proposal for {name} waiting."}
        if len(open_) >= 5:
            return {"status": "refused", "summary": "You have 5 proposals waiting; wait for the user."}
        p = SkillProposal(skill_id=base.id if base else None, name=name, title=title, when_to_use=when_to_use,
                          body=steps, tools=list(tools or []), script=script,
                          scope="agent" if only_for_me else (base.scope if base else "team"),
                          reason=reason.strip(), source=source, agent_id=me.id, agent_name=me.name,
                          thread_id=thread_id, job_id=job_id,
                          base_version=base.version if base else None)
        what = f"an improvement to the skill {name}" if base else f"a new skill: {title}"
        cases = evals.cases_for_proposal(me, name) if evals.get_settings()["on_proposals"] else []
        if mode == LearningMode.auto and cases and evals.get_settings()["gate_auto"]:
            # Test it first: applied if it doesn't lower the pass rate, else it waits for you.
            p.status = "testing"
            if thread_id:
                msg = await self.save_message(thread_id, Role.event,
                                              f"{me.name} is testing {what} before adding it{via_note}",
                                              agent_id=me.id, kind="skill_proposal", proposal_id=p.id,
                                              status="testing", agent=me.name, depth=depth)
                p.message_id = msg.id
            with db.session() as s:
                s.add(p)
                s.commit()
            evals.queue_proposal_run(p, cases)
            return {"status": "testing", "summary": "Testing the change against your test cases first; "
                    "it's applied if it doesn't make them worse. Carry on with the task.", "proposal_id": p.id}
        if mode == LearningMode.auto:
            k = skills.apply_proposal(p, by=me.name)
            p.status, p.decided_at, p.skill_id = "applied", _now(), k.id
            if thread_id:
                msg = await self.save_message(thread_id, Role.event,
                                              f"{me.name} added {what} (learning mode: auto){via_note}",
                                              agent_id=me.id, kind="skill_proposal", proposal_id=p.id,
                                              status="applied", agent=me.name, depth=depth)
                p.message_id = msg.id
            with db.session() as s:
                s.add(p)
                s.commit()
            await subagents.notify({"type": "skill_applied", "proposal_id": p.id, "agent": me.name,
                                    "skill": name, "version": k.version})
            return {"status": "applied", "summary": f"Saved {name} (version {k.version}).", "proposal_id": p.id}
        if thread_id:
            msg = await self.save_message(thread_id, Role.event, f"{me.name} proposes {what}{via_note}",
                                          agent_id=me.id, kind="skill_proposal", proposal_id=p.id,
                                          status="pending", agent=me.name, depth=depth)
            p.message_id = msg.id
        with db.session() as s:
            s.add(p)
            s.commit()
        if thread_id:
            await self.broker.publish(thread_id, "skill_proposed", proposal_id=p.id,
                                      agent=me.name, skill=name, depth=depth)
        if cases:
            evals.queue_proposal_run(p, cases)
        await subagents.notify({"type": "skill_proposal", "proposal_id": p.id, "agent": me.name,
                                "skill": name, "title": title, "revision": bool(base),
                                "reason": reason, "link": subagents.link(f"/requests/skills?skill={p.id}")})
        return {"status": "pending", "summary": "Sent to the user for review. Carry on with the task.",
                "proposal_id": p.id}

    async def decide_skill_proposal(self, proposal_id: str, approve: bool, *, note: str = "",
                                    edits: dict[str, Any] | None = None, by: str = "you") -> SkillProposal:
        with db.session() as s:
            p = s.get(SkillProposal, proposal_id)
            if p is None:
                raise LookupError("no such proposal")
            if p.status not in ("pending", "testing"):
                raise ValueError(f"this proposal is already {p.status}")
            for k, v in (edits or {}).items():
                if k in ("name", "title", "when_to_use", "body", "tools", "script", "scope") and v is not None:
                    setattr(p, k, v)
        if approve:
            why = skills.check_fields(p.name, p.title, p.when_to_use, p.body)
            if why:
                raise ValueError(why)
            k = skills.apply_proposal(p, by=by)
            p.skill_id = k.id
        p.status = "approved" if approve else "rejected"
        p.note, p.decided_at = note.strip(), _now()
        with db.session() as s:
            s.add(p)
            s.commit()
            s.refresh(p)
        if p.message_id:
            await self.update_message(p.message_id, status=p.status, note=p.note)
        return p

    async def suggest_profile(self, me: AgentDef, text: str, reason: str, *, thread_id: str,
                              job_id: str | None = None, reflection_id: str | None = None) -> ProfileProposal | None:
        """An agent suggests a line for your profile: it waits in Requests → Profile updates."""
        text = " ".join(text.split())[:500]
        if not text or profile.already_known(text):
            return None
        p = ProfileProposal(text=text, reason=reason.strip()[:1000], agent_id=me.id, agent_name=me.name,
                            thread_id=thread_id, job_id=job_id, reflection_id=reflection_id)
        msg = await self.save_message(
            thread_id, Role.event, f"{me.name} suggests adding to your profile: {text}",
            agent_id=me.id, kind="profile_proposal", profile_proposal_id=p.id, status="pending")
        p.message_id = msg.id
        with db.session() as s:
            s.add(p)
            s.commit()
            s.refresh(p)
        await subagents.notify({"type": "profile_proposal", "proposal_id": p.id, "agent": me.name,
                                "text": text, "thread_id": thread_id,
                                "link": subagents.link(f"/requests/profile?suggestion={p.id}")})
        return p

    async def suggest_knowledge(self, me: AgentDef, *, title: str, text: str, reason: str,
                                collection: str = "", thread_id: str, job_id: str | None = None,
                                attachment_id: str | None = None, source: str = "tool",
                                reflection_id: str | None = None) -> dict:
        """An agent suggests a document for the knowledge library: it waits for you in
        Requests → Knowledge and as a card in the conversation."""
        try:
            target = kprop.resolve_target(me, collection)
            title, text = kprop.clean(title, text)
        except ValueError as e:
            return {"status": "not suggested", "summary": str(e)}
        if kprop.pending_count(me.id) >= kprop.MAX_PENDING_PER_AGENT:
            return {"status": "not suggested", "summary": f"you already have {kprop.MAX_PENDING_PER_AGENT} "
                    "suggestions waiting for the user; wait for their decisions"}
        dup = kprop.already_suggested(target.id, title, text)
        if dup is not None:
            return {"status": "not suggested", "summary": f"this was suggested already ({dup.status})"
                    + (f"; the user's note: {dup.note}" if dup.note else "")}
        similar, score = await kprop.closest(target.id, title, text)
        p = KnowledgeProposal(collection_id=target.id, title=title, text=text,
                              reason=reason.strip()[:1000], attachment_id=attachment_id, source=source,
                              agent_id=me.id, agent_name=me.name, thread_id=thread_id, job_id=job_id,
                              reflection_id=reflection_id,
                              similar_doc_id=similar.id if similar else None,
                              similar_title=similar.title if similar else "", similar_score=score)
        msg = await self.save_message(
            thread_id, Role.event, f"{me.name} suggests adding “{title}” to {target.name}",
            agent_id=me.id, kind="knowledge_proposal", knowledge_proposal_id=p.id, status="pending",
            collection=target.name)
        p.message_id = msg.id
        with db.session() as s:
            s.add(p)
            s.commit()
            s.refresh(p)
        await subagents.notify({"type": "knowledge_proposal", "proposal_id": p.id, "agent": me.name,
                                "title": title, "collection": target.name, "thread_id": thread_id,
                                "link": subagents.link(f"/requests/knowledge?document={p.id}")})
        out = {"status": "suggested", "proposal_id": p.id, "collection": target.name,
               "summary": "It waits for the user's approval. Mention it in one line."}
        if similar is not None:
            out["note"] = f"It looks close to “{similar.title}”, already in {target.name}; the user will see that."
        return out

    async def decide_knowledge_proposal(self, proposal_id: str, approve: bool, *, note: str = "",
                                        title: str | None = None, text: str | None = None,
                                        collection_id: str | None = None,
                                        replace_doc_id: str | None = None) -> KnowledgeProposal:
        p = await kprop.decide(proposal_id, approve, note=note, title=title, text=text,
                               collection_id=collection_id, replace_doc_id=replace_doc_id)
        if p.message_id:
            with db.session() as s:
                c = s.get(KnowledgeCollection, p.collection_id)
            await self.update_message(p.message_id, status=p.status, note=p.note, doc_id=p.doc_id,
                                      collection=c.name if c else "")
        return p

    def _knowledge_suggest_toolset(self) -> FunctionToolset:
        rt = self

        async def knowledge_targets(ctx: RunContext[RunDeps]) -> list[dict]:
            """The knowledge collections you may suggest documents for (the first is the default)."""
            me = load_agent(ctx.deps.agent_id)
            return [{"collection": c.name, "description": c.description} for c in kprop.targets(me)]

        async def suggest_knowledge(ctx: RunContext[RunDeps], title: str, reason: str,
                                    content: str = "", attachment_id: str = "",
                                    collection: str = "") -> dict:
            """Suggest a document for the knowledge library. The user approves it first.

            Args:
                title: A clear title, e.g. "UPS failover runbook".
                reason: Why it's worth keeping for the team.
                content: The document (Markdown), complete and self-contained. Or leave it
                    empty and give attachment_id to keep the text of a file instead.
                attachment_id: A file in this conversation to keep, e.g. att_ab12cd34ef56.
                collection: Only when the user asked for a particular collection (see
                    knowledge_targets). Empty means Agent Findings.
            """
            me = load_agent(ctx.deps.agent_id)
            if ctx.deps.replay is not None:  # a test run: nothing is stored
                return {"status": "suggested", "proposal_id": "test", "summary": "(test run)"}
            text, att_id = content, None
            if attachment_id.strip():
                try:
                    (a,) = att.in_thread(ctx.deps.thread_id, [attachment_id])
                except att.AttachmentError as e:
                    return {"status": "not suggested", "summary": str(e)}
                if a.text is None:
                    return {"status": "not suggested", "summary": f"{a.name} has no text to keep"}
                text, att_id = (content.strip() + "\n\n" if content.strip() else "") + a.text, a.id
            return await rt.suggest_knowledge(me, title=title, text=text, reason=reason,
                                              collection=collection, thread_id=ctx.deps.thread_id,
                                              job_id=ctx.deps.job_id, attachment_id=att_id)

        return FunctionToolset([knowledge_targets, suggest_knowledge], id="knowledge_suggestions")

    async def decide_profile_proposal(self, proposal_id: str, approve: bool, *, note: str = "",
                                      text: str | None = None) -> ProfileProposal:
        p = profile.decide(proposal_id, approve, note=note, text=text)
        if p.message_id:
            await self.update_message(p.message_id, status=p.status, note=p.note)
        return p

    # ------------------------------------------------------------ tool approvals

    def _with_approvals(self, ts, srv: ToolServer):
        return ApprovalToolset(ts, rt=self, server_id=srv.id, server_name=srv.name,
                               rules=dict(srv.approval_rules or {}), read_only=bool(srv.read_only),
                               eval_live=dict(srv.eval_live or {}))

    async def ask_approval(self, deps: RunDeps, server_id: str, server_name: str, tool: str,
                           args: dict, reason: str = "") -> dict:
        """Post an approval card and wait for your decision (or the timeout)."""
        agent = load_agent(deps.agent_id)
        row = ToolApproval(agent_id=agent.id, agent_name=agent.name, thread_id=deps.thread_id,
                           job_id=deps.job_id, server_id=server_id, server_name=server_name,
                           tool=tool, args=args or {}, reason=reason)
        msg = await self.save_message(
            deps.thread_id, Role.event, f"{agent.name} wants to run {tool} on {server_name}",
            agent_id=agent.id, kind="tool_approval", approval_id=row.id, status="pending",
            agent=agent.name, tool=tool, server=server_name, depth=deps.depth,
            **({"reason": reason} if reason else {}))
        row.message_id = msg.id
        with db.session() as s:
            s.add(row)
            s.commit()
        await self.broker.publish(deps.thread_id, "approval_requested", approval_id=row.id,
                                  agent=agent.name, tool=tool, server=server_name, depth=deps.depth)
        await subagents.notify({"type": "tool_approval", "approval_id": row.id, "agent": agent.name,
                                "tool": tool, "server": server_name, "thread_id": deps.thread_id,
                                **({"reason": reason} if reason else {}),
                                "args": {k: (str(v)[:500]) for k, v in (args or {}).items()},
                                "link": subagents.link(f"/requests/approvals?approval={row.id}")})
        wait = approvals.timeout_seconds()
        workitems.mark_waiting(deps.thread_id, "approval", f"{agent.name} wants to run {tool}")
        try:
            async with released_slot():  # don't hold a worker slot while you decide
                got = await self.broker.r.blpop([approvals.key(row.id)], timeout=wait)
        finally:
            workitems.clear_waiting(deps.thread_id, "approval")
        with db.session() as s:
            row = s.get(ToolApproval, row.id)
            if row.status == "pending":  # nobody decided in time
                row.status, row.decided_at = "expired", _now()
                s.add(row)
                s.commit()
                s.refresh(row)
        if got is None and row.status == "expired":
            await self.update_message(row.message_id, status="expired")
            return {"status": "expired",
                    "message": f"Not run: the user didn't approve {tool} within "
                               f"{wait / 60:g} minutes. Report that it's waiting for approval."}
        if row.status == "approved":
            return {"status": "approved", "message": ""}
        note = f" The user's note: {row.note}" if row.note else ""
        return {"status": "denied",
                "message": f"Not run: the user denied this call to {tool}.{note} "
                           "Don't retry it or work around it."}

    async def decide_approval(self, approval_id: str, approve: bool, *, note: str = "",
                              remember_minutes: int = 0) -> ToolApproval:
        with db.session() as s:
            row = s.get(ToolApproval, approval_id)
            if row is None:
                raise LookupError("no such approval")
            if row.status != "pending":
                raise ValueError(f"this call is already {row.status}")
            row.status = "approved" if approve else "denied"
            row.note = note.strip()
            row.decided_at = _now()
            if approve and remember_minutes > 0:
                row.remember_until = _now() + timedelta(minutes=remember_minutes)
            s.add(row)
            s.commit()
            s.refresh(row)
        # Wake the waiting tool call (it expires on its own if nobody is waiting any more).
        await self.broker.r.rpush(approvals.key(row.id), row.status)
        await self.broker.r.expire(approvals.key(row.id), 3600)
        if row.message_id:
            await self.update_message(row.message_id, status=row.status, note=row.note,
                                      remember_until=row.remember_until.isoformat()
                                      if row.remember_until else None)
        await subagents.notify({"type": "tool_approval_decision", "approval_id": row.id,
                                "agent": row.agent_name, "tool": row.tool, "status": row.status,
                                "note": row.note})
        return row

    # ------------------------------------------------------------ sub-agent tools

    async def delegate_action(self, deps: RunDeps, me: AgentDef, target: AgentDef, *,
                              task: str, shown: str, extra_meta: dict[str, Any]) -> Job:
        """Queue a background task for one of `me`'s agents, shown in the conversation as a
        delegation card (`shown` is the short version). Used by goal teams
        (app/workitems.py delegate_action): with `goal_action` in its meta, the result goes
        onto that action instead of into a report turn."""
        chain = list(getattr(deps, "chain", None) or [me.id])
        depth = int(getattr(deps, "depth", 0) or 0) + 1
        job = Job(
            kind=JobKind.task, origin_thread_id=deps.thread_id, requester_agent_id=me.id,
            assignee_agent_id=target.id, parent_job_id=getattr(deps, "job_id", None), task=task,
            background=True, depth=depth, chain=[*chain, target.id], pool=target.pool,
            meta={**extra_meta, **({"cautious": True} if getattr(deps, "cautious", False) else {})})
        msg = await self.save_message(
            deps.thread_id, Role.event, f"{me.name} → {target.name}: {shown}",
            agent_id=target.id, kind="delegation_result", job_id=job.id, status="queued",
            task=shown, requester_id=me.id, requester=me.name, assignee=target.name,
            background=True, depth=depth, started_at=_now().isoformat())
        job.meta = {**(job.meta or {}), "message_id": msg.id}
        return await self._submit(job)

    async def spawn(self, deps: RunDeps, me: AgentDef, *, role: str, task: str,
                    why_not_existing_agents: str, instructions: str = "", model: str | None = None,
                    tools: list[str] | None = None, background: bool = False,
                    extra_meta: dict[str, Any] | None = None, check_text: str | None = None) -> dict:
        """Create a sub-agent and queue its task, after every check (mode, grant, duplicates,
        models, tools, limits). Returns {"status": "refused", "summary"} or
        {"status": "queued", "job", "sub", "minutes"}. Used by spawn_subagent and by goal
        teams (app/workitems.py assign_action).

        `check_text` is what the duplicate check compares with your agents' descriptions
        when `task` carries more than the work itself (a goal team member's briefing also
        has the goal and what the team found, which would water the comparison down)."""
        _sa = subagents
        if _sa.mode_of(me) == SubagentMode.off:
            return {"status": "refused", "summary": "You can't create sub-agents."}
        role, task = role.strip()[:60], task.strip()
        if not role or not task:
            return {"status": "refused", "summary": "Give the sub-agent a role and a task."}
        if len(why_not_existing_agents.strip()) < 10:
            return {"status": "refused", "summary": "Explain in why_not_existing_agents why "
                    "none of the existing agents can do this."}
        grant = None
        if _sa.mode_of(me) == SubagentMode.ask:
            grant = _sa.active_grant(me.id, deps.thread_id)
            if grant is None:
                return {"status": "refused", "summary": "You need the user's approval first: "
                        "call request_subagents and wait for the decision."}
            limits = _sa.grant_limits(grant)
        else:
            limits = _sa.limits_of(me)
        dup, score = await _sa.duplicate_of(role, (check_text or task).strip(), me)
        # An approved request already showed the user any likely overlap, so only an
        # outright name clash is refused under a grant.
        if dup and (grant is None or score >= 1.0):
            return self._duplicate_refusal(me, role, dup)
        options = _sa.model_options(me, limits)
        picked = _sa.pick_model(options, model)
        if picked is None:
            return {"status": "refused", "summary": f"Model '{model}' isn't allowed for sub-agents. "
                    f"Choose from: {', '.join(o['label'] for o in options) or 'none (ask the user)'}."}
        allowed_tools = _sa.tool_options(me, limits)
        chosen, bad = _sa.pick_tools(allowed_tools, tools)
        if bad:
            return {"status": "refused", "summary": f"Not allowed for sub-agents: {', '.join(bad)}. "
                    f"You may give: {', '.join(t.name for t in allowed_tools) or 'no tool servers'}."}

        team = _sa.get_settings()
        async with self.broker.r.lock("ahq:lock:subagents", timeout=30, blocking_timeout=30):
            _sa.sweep()
            grant_left = None
            if grant is not None:
                with db.session() as s:
                    g = s.get(SubagentRequest, grant.id)
                grant_left = int((g.grant or {}).get("count", 0)) - g.used
            why = _sa.check_limits(_sa.counts(me.id, deps.job_id), limits, team, grant_left)
            if why:
                return {"status": "refused", "summary": f"Sub-agent not created: {why}. "
                        "Do the work yourself, delegate it, or wait for running sub-agents to finish."}
            minutes = int(limits.get("max_minutes") or 10)
            sub = AgentDef(
                name="", description=f"{role}: {task[:300]}", pronouns="they/them",
                instructions=(instructions.strip() or f"You are {role}, a helper for {me.name}."),
                provider_id=picked["provider_id"], model=picked["model"], pool=me.pool,
                tool_server_ids=[t.id for t in chosen], delegates=[], memory_enabled=False,
                thinking=me.thinking or "",  # sub-agents think as hard as the agent that made them
                is_subagent=True, parent_agent_id=me.id, spawned_by_job_id=deps.job_id,
                subagent_limits={"max_minutes": minutes,
                                 "max_requests": int(limits.get("max_requests") or 25)},
                expires_at=_now() + timedelta(minutes=minutes + _sa.GRACE_MINUTES))
            sub.name = f"{role} ({me.name} #{sub.id[:4]})"
            used = None
            with db.session() as s:
                s.add(sub)
                if grant is not None:
                    g = s.get(SubagentRequest, grant.id)
                    g.used += 1
                    used = g.used
                    s.add(g)
                s.commit()
                s.refresh(sub)
        if grant is not None and grant.message_id:
            await self.update_message(grant.message_id, used=used)  # the approval card shows it

        job = Job(kind=JobKind.task, origin_thread_id=deps.thread_id, requester_agent_id=me.id,
                  assignee_agent_id=sub.id, parent_job_id=deps.job_id, task=task,
                  background=background, depth=deps.depth + 1,
                  chain=[*deps.chain, sub.id], pool=sub.pool,
                  meta={"cautious": True} if deps.cautious else {})
        msg = await self.save_message(
            deps.thread_id, Role.event, f"{me.name} → {sub.name}: {task}",
            agent_id=me.id, kind="delegation_result", job_id=job.id, status="queued",
            task=task, requester_id=me.id, requester=me.name, assignee=sub.name,
            background=background, depth=job.depth, started_at=_now().isoformat(),
            subagent=True, subagent_role=role, model=picked["label"],
            tools=[t.name for t in chosen], max_minutes=minutes,
            why_not_existing_agents=why_not_existing_agents.strip(),
            request_id=grant.id if grant else None)
        job.meta = {**(job.meta or {}), "message_id": msg.id, "subagent": True, **(extra_meta or {})}
        job = await self._submit(job)
        await self.broker.publish(deps.thread_id, "subagent_spawned", agent=me.name,
                                  subagent=sub.name, role=role, model=picked["label"],
                                  job_id=job.id, depth=deps.depth)
        return {"status": "queued", "job": job, "sub": sub, "minutes": minutes}

    def _subagent_toolset(self, mode: SubagentMode) -> FunctionToolset:
        rt = self
        _sa = subagents

        async def spawn_subagent(ctx: RunContext[RunDeps], role: str, task: str,
                                 why_not_existing_agents: str, instructions: str = "",
                                 model: str | None = None, tools: list[str] | None = None,
                                 background: bool = False) -> dict:
            """Create a temporary sub-agent for one self-contained part of your task and get its result.

            Args:
                role: Short role name, e.g. "Feed reader" (not the name of an existing agent).
                task: Everything it needs to know: what to do and what to return. It doesn't see this chat.
                why_not_existing_agents: Why none of the existing agents can do this (required).
                instructions: Optional extra system instructions for it (how to work, what to avoid).
                model: One of the models listed in your instructions (default: the first).
                tools: Tool server names to give it (default: all you're allowed to give).
                background: False (default) waits for its result. True returns at once; you're
                    woken in this conversation when it finishes.
            """
            me = load_agent(ctx.deps.agent_id)
            got = await rt.spawn(ctx.deps, me, role=role, task=task,
                                 why_not_existing_agents=why_not_existing_agents,
                                 instructions=instructions, model=model, tools=tools,
                                 background=background)
            if got["status"] != "queued":
                return got
            job, sub, minutes = got["job"], got["sub"], got["minutes"]
            if background:
                return {"status": "queued", "job_id": job.id, "subagent": sub.name,
                        "summary": f"Sub-agent {sub.name} is working on it. You'll be woken in "
                        "this conversation with its result."}
            wait = min(settings.delegate_wait_timeout, (minutes + _sa.GRACE_MINUTES) * 60)
            async with released_slot():
                result = await rt.broker.wait_result(job.id, wait)
            if result is None:
                job = update_job(job.id, background=True)
                if job.status in (JobStatus.done, JobStatus.failed) and job.result:
                    result = job.result
                else:
                    return {"status": "queued", "job_id": job.id,
                            "summary": f"{sub.name} is still working; it now runs in the background "
                            "and you'll be woken with its result."}
            return {"from": sub.name, **result,
                    "note": f"This is the report of {sub.name}, a sub-agent you created. Check it, "
                            "and tell whoever asked you that you used a sub-agent for this part."}

        async def request_subagents(ctx: RunContext[RunDeps], why: str,
                                    why_not_existing_agents: str,
                                    subagents: list[PlannedSubagent], hours: float = 4) -> dict:
            """Ask the user for sub-agents, like a staffing request: which, why, and how much.

            Args:
                why: What the sub-agents are for and why the task needs them.
                why_not_existing_agents: Why none of the existing agents can do this.
                subagents: The sub-agents you want (at most 5).
                hours: How long you need the approval for (0.5 to 72).
            """
            me = load_agent(ctx.deps.agent_id)
            if _sa.mode_of(me) != SubagentMode.ask:
                return {"status": "refused", "summary": "Requests aren't needed or allowed for you."}
            plans = subagents
            if not plans or len(plans) > 5:
                return {"status": "refused", "summary": "Ask for between 1 and 5 sub-agents."}
            if len(why.strip()) < 10 or len(why_not_existing_agents.strip()) < 10:
                return {"status": "refused", "summary": "Explain both why you need them and why "
                        "none of the existing agents can do it."}
            team = _sa.get_settings()
            if _sa.pending_count(me.id) >= int(team["max_pending_per_agent"]):
                return {"status": "refused", "summary": "You already have requests waiting for the "
                        "user. Wait for a decision before asking again."}
            lim = _sa.limits_of(me)
            options = _sa.model_options(me, lim)
            tool_opts = _sa.tool_options(me, lim)
            rows = []
            for plan in plans:
                dup, _ = await _sa.duplicate_of(plan.role, plan.purpose, me)
                if dup:
                    return rt._duplicate_refusal(me, plan.role, dup)
                picked = _sa.pick_model(options, plan.model)
                if picked is None:
                    return {"status": "refused", "summary": f"Model '{plan.model}' isn't available "
                            f"for sub-agents. Choose from: {', '.join(o['label'] for o in options)}."}
                chosen, bad = _sa.pick_tools(tool_opts, plan.tools)
                if bad:
                    return {"status": "refused", "summary": f"Not available for sub-agents: "
                            f"{', '.join(bad)}. Available: {', '.join(t.name for t in tool_opts) or 'none'}."}
                rows.append({"role": plan.role.strip()[:60], "purpose": plan.purpose.strip(),
                             "provider_id": picked["provider_id"], "model": picked["model"],
                             "label": picked["label"], "tools": [t.name for t in chosen],
                             "tool_server_ids": [t.id for t in chosen],
                             "est_minutes": max(1, min(int(plan.est_minutes or 10), 120))})
            hours = max(0.5, min(float(hours or 4), 72))
            concurrent = min(len(rows), max(1, int(lim["concurrent"])))
            est = await _sa.estimate(me, rows, concurrent=concurrent)
            req = SubagentRequest(agent_id=me.id, thread_id=ctx.deps.thread_id, job_id=ctx.deps.job_id,
                                  why=why.strip(), why_not_existing_agents=why_not_existing_agents.strip(),
                                  subagents=rows, hours=hours, estimate=est,
                                  expires_at=_now() + timedelta(hours=float(team["request_ttl_hours"])))
            with db.session() as s:
                s.add(req)
                s.commit()
                s.refresh(req)
            roles = ", ".join(r["role"] for r in rows)
            msg = await rt.save_message(
                ctx.deps.thread_id, Role.event,
                f"{me.name} is asking for {len(rows)} sub-agent(s): {roles}. {why.strip()}",
                agent_id=me.id, kind="subagent_request", request_id=req.id, status="pending",
                agent=me.name, depth=ctx.deps.depth)
            with db.session() as s:
                r = s.get(SubagentRequest, req.id)
                r.message_id = msg.id
                s.add(r)
                s.commit()
            await rt.broker.publish(ctx.deps.thread_id, "subagent_request", request_id=req.id,
                                    agent=me.name, depth=ctx.deps.depth)
            await _sa.notify({"type": "subagent_request", "request_id": req.id, "agent": me.name,
                              "thread_id": ctx.deps.thread_id, "why": req.why,
                              "subagents": [{k: r[k] for k in ("role", "purpose", "label", "est_minutes")}
                                            for r in rows],
                              "total_minutes": est["total_minutes"], "hours": hours,
                              "link": _sa.link(f"/requests/subagents?id={req.id}")})
            if ctx.deps.depth == 0:
                return {"status": "pending", "request_id": req.id,
                        "summary": "Sent to the user for approval. You'll be woken in this "
                        "conversation with the decision. Tell the user briefly what you asked "
                        "for and why, then stop and wait."}
            return {"status": "pending", "request_id": req.id,
                    "summary": "Sent to the user for approval. Return status 'partial' now, saying "
                    "you've asked the user for sub-agents (and why) and are waiting. If it's "
                    "approved, the task can be delegated to you again and you'll be able to use them."}

        tools = [spawn_subagent] + ([request_subagents] if mode == SubagentMode.ask else [])
        return FunctionToolset(tools, id="subagents")

    @staticmethod
    def _duplicate_refusal(me: AgentDef, role: str, dup: AgentDef) -> dict:
        on_team = find_delegate(me, dup.name) is not None
        nxt = (f"Delegate this to {dup.name} instead." if on_team else
               f"{dup.name} isn't on your team: tell whoever asked you that {dup.name} is the "
               "agent for this.")
        return {"status": "refused",
                "summary": f"Not created: '{role}' would duplicate {dup.name}, an agent that "
                f"already exists ({(dup.description or '').strip()[:160]}). {nxt}"}

    async def decide_request(self, request_id: str, approve: bool, *, note: str = "",
                             changes: dict[str, Any] | None = None) -> SubagentRequest:
        """Your decision on a sub-agent request. Wakes the conversation it came from."""
        changes = changes or {}
        with db.session() as s:
            req = s.get(SubagentRequest, request_id)
            if req is None:
                raise LookupError("no such request")
            if req.status != "pending":
                raise ValueError(f"this request is already {req.status}")
            if req.expires_at and subagents.aware(req.expires_at) < _now():
                req.status = "expired"
                s.add(req)
                s.commit()
                raise ValueError("this request has expired")
            agent = s.get(AgentDef, req.agent_id)
            lim = subagents.limits_of(agent)
            if approve:
                models = changes.get("models")
                if models is None:
                    seen, models = set(), []
                    for r in req.subagents:
                        k = (r.get("provider_id"), r.get("model"))
                        if k not in seen:
                            seen.add(k)
                            models.append({"provider_id": k[0], "model": k[1]})
                tool_ids = changes.get("tool_server_ids")
                if tool_ids is None:
                    tool_ids = sorted({i for r in req.subagents for i in r.get("tool_server_ids", [])})
                hours = float(changes.get("hours") or req.hours)
                count = int(changes.get("count") or len(req.subagents))
                req.grant = {
                    "count": count,
                    "concurrent": int(changes.get("concurrent") or min(count, max(1, int(lim["concurrent"])))),
                    "max_minutes": int(changes.get("max_minutes") or max(
                        [int(r.get("est_minutes", 10)) for r in req.subagents] or [10])),
                    "max_requests": int(changes.get("max_requests") or lim["max_requests"]),
                    "models": models, "tool_server_ids": tool_ids, "hours": hours}
                req.status = "approved"
                req.expires_at = _now() + timedelta(hours=hours)
            else:
                req.status = "denied"
            req.note = note.strip()
            req.decided_at = _now()
            s.add(req)
            s.commit()
            s.refresh(req)
            thread = s.get(Thread, req.thread_id)
            origin = s.get(Job, req.job_id) if req.job_id else None
        if req.message_id:
            await self.update_message(req.message_id, status=req.status, grant=req.grant,
                                      note=req.note, decided_at=req.decided_at.isoformat())
        await subagents.notify({"type": "subagent_decision", "request_id": req.id,
                                "agent": agent.name, "status": req.status, "grant": req.grant,
                                "note": req.note, "thread_id": req.thread_id})
        await self.enqueue_turn(req.thread_id, self._decision_prompt(req, agent, thread, origin),
                                from_user=False,
                                event_text=f"You {req.status} {agent.name}'s sub-agent request"
                                + (f": {req.note}" if req.note else "."),
                                kind="subagent_decision", request_id=req.id)
        return req

    @staticmethod
    def _decision_prompt(req: SubagentRequest, agent: AgentDef, thread: Thread | None,
                         origin: Job | None) -> str:
        own = thread is not None and thread.agent_id == agent.id
        note = f"\nThe user's note: {req.note}" if req.note else ""
        if req.status == "approved":
            g = req.grant or {}
            models = ", ".join(subagents.model_label(m.get("provider_id"), m.get("model", ""))
                               for m in g.get("models", [])) or "the requester's own model"
            terms = (f"up to {g['count']} sub-agent(s), {g['concurrent']} at once, "
                     f"{g['max_minutes']} minutes and {g['max_requests']} model requests each, "
                     f"on {models}, valid for {g['hours']:g} hours")
            if own:
                return (f"[Sub-agent request {req.id}: approved by the user]\nGrant: {terms}.{note}\n"
                        "Continue the task you asked for them for, using spawn_subagent within "
                        "this grant. Report the outcome to the user.")
            task = f"\n{agent.name} had been asked: {origin.task}" if origin else ""
            return (f"[Sub-agent request {req.id} from {agent.name}: approved by the user]\n"
                    f"Grant: {terms}.{note}{task}\n"
                    f"Delegate that task to {agent.name} again now; {agent.name} can use the "
                    "approved sub-agents in this conversation. Then report the outcome.")
        if own:
            return (f"[Sub-agent request {req.id}: denied by the user]{note}\n"
                    "Continue without sub-agents: do the work yourself or delegate to your team, "
                    "and tell the user how you'll proceed.")
        return (f"[Sub-agent request {req.id} from {agent.name}: denied by the user]{note}\n"
                f"{agent.name} won't get sub-agents. Decide how to proceed with the task and "
                "tell the user.")



# --------------------------------------------------------------------------- checkpointer


@contextlib.asynccontextmanager
async def open_checkpointer():
    """Postgres checkpointer when AHQ_DB_URL is Postgres, else a local SQLite file."""
    if settings.is_postgres:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(settings.checkpoint_url) as saver:
            await saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_url) as saver:
            yield saver
