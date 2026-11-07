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
import contextlib
import json
import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from pydantic_ai import (
    Agent,
    FunctionToolset,
    ModelMessagesTypeAdapter,
    PrefixedToolset,
    RunContext,
    WrapperToolset,
)
from pydantic_ai.mcp import MCPToolset
from redis.exceptions import LockError
from sqlmodel import select

from . import db
from .broker import Broker
from .config import settings
from .llm import build_agent_model, build_model_for, describe_model
from . import memory
from .telemetry import current_traceparent, job_span, set_output
from .models import (
    AgentDef,
    Job,
    JobKind,
    JobStatus,
    Message,
    Role,
    MemoryKind,
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
        rows = s.exec(select(AgentDef).where(AgentDef.enabled == True)).all()  # noqa: E712
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
        return MCPToolset(srv.url, id=srv.name)
    if not srv.command:
        raise ValueError(f"tool server {srv.name} has no command")
    config = {"mcpServers": {srv.name: {"command": srv.command, "args": srv.args, "env": srv.env}}}
    return MCPToolset(config, id=srv.name)


def agent_tool_servers(agent: AgentDef) -> list[ToolServer]:
    if not agent.tool_server_ids:
        return []
    with db.session() as s:
        servers = [s.get(ToolServer, i) for i in agent.tool_server_ids]
    return [srv for srv in servers if srv is not None and srv.enabled]


def desktop_servers(agent: AgentDef) -> list[ToolServer]:
    return [srv for srv in agent_tool_servers(agent) if srv.desktop]


def build_tool_toolsets(agent: AgentDef) -> list[tuple[Any, str | None]]:
    """[(toolset, desktop server id or None)] for the agent's MCP servers."""
    out = []
    for srv in agent_tool_servers(agent):
        ts = mcp_toolset(srv)
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
  to read the page as text and act on elements by their ref. Use `look` only for things
  that are genuinely visual, such as charts and graphs."""

ESCALATE_RULES = """\
If the task is beyond you (you keep failing, or it needs stronger reasoning), call
`escalate` with a short reason instead of guessing. A stronger model will take over."""


class Escalation(Exception):
    """Raised by the escalate tool to hand the run to the agent's fallback model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _find_exc(exc: BaseException, kind: type) -> BaseException | None:
    if isinstance(exc, kind):
        return exc
    for sub in getattr(exc, "exceptions", ()) or ():
        found = _find_exc(sub, kind)
        if found:
            return found
    cause = exc.__cause__ or exc.__context__
    return _find_exc(cause, kind) if cause is not None and cause is not exc else None


def has_vision(agent: AgentDef) -> bool:
    return bool(agent.vision_model) and bool(desktop_servers(agent))


def memory_on(agent: AgentDef) -> bool:
    return agent.memory_enabled and memory.get_settings()["enabled"]


MEMORY_RULES = """\
Long-term memory:
- Relevant memories may be listed below. Treat them as leads to verify, not facts.
- Use `remember` for things worth knowing next time (how the environment is set up, what
  fixed a problem, the user's preferences). One clear, self-contained statement each.
- Use `forget` when you find a memory is wrong or outdated, `confirm` when you verify one,
  and `recall` to search for more."""


def has_fallback(agent: AgentDef) -> bool:
    return bool(agent.fallback_model)


DESKTOP_PAUSED = ("The user has taken control of this desktop, so desktop tools are paused. "
                  "Do not retry. Report that you are waiting for the user to hand the desktop "
                  "back; you will be woken when they do.")


def attributed(name: str, result: dict) -> dict:
    """A delegate's result, labelled so the delegator relays it as theirs, not its own."""
    return {"from": name, **result,
            "note": f"This is {name}'s report to you, not your own work. Tell whoever asked "
                    f"you that you delegated this to {name} and what {name} reported."}


def team_roster(agent: AgentDef) -> str:
    team = allowed_delegates(agent)
    if not team:
        return ""
    lines = [f"- {t.name}: {t.description or '(no description)'}" for t in team]
    return GENERIC_RULES + "\nTeam members you can delegate to:\n" + "\n".join(lines)


@dataclass
class ObservedToolset(WrapperToolset):
    """Publishes every tool call and result to the conversation's live event stream."""

    agent_name: str = ""
    broker: Broker | None = None
    desktop_server_id: str | None = None

    async def call_tool(self, name, tool_args, ctx: RunContext[RunDeps], tool):
        d = ctx.deps
        if self.desktop_server_id and await self.broker.user_has_control(self.desktop_server_id):
            await self.broker.mark_blocked(self.desktop_server_id,
                                           {"agent_id": d.agent_id, "thread_id": d.thread_id})
            await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                      agent_id=d.agent_id, tool=name, depth=d.depth,
                                      error="paused: you have control of this desktop")
            return DESKTOP_PAUSED
        await self.broker.publish(d.thread_id, "tool_call", agent=self.agent_name,
                                  agent_id=d.agent_id, tool=name, args=tool_args, depth=d.depth)
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
        except Exception as e:
            await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                      agent_id=d.agent_id, tool=name, error=str(e), depth=d.depth)
            raise
        await self.broker.publish(d.thread_id, "tool_result", agent=self.agent_name,
                                  agent_id=d.agent_id, tool=name, preview=str(result)[:500],
                                  depth=d.depth)
        return result


# --------------------------------------------------------------------------- runtime


class TurnState(TypedDict, total=False):
    agent_id: str
    prompt: str
    job_id: str
    history: list[dict]
    reply: str


class Runtime:
    """Shared by the API process (which only enqueues) and workers (which execute).

    Pass a LangGraph checkpointer in processes that run turns (workers).
    """

    def __init__(self, broker: Broker, checkpointer=None) -> None:
        self.broker = broker
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
            s.commit()
            s.refresh(m)
        await self.broker.publish(thread_id, "message", message=json.loads(m.model_dump_json()))
        return m

    # ------------------------------------------------------------ enqueueing

    async def enqueue_turn(self, thread_id: str, prompt: str, *, from_user: bool = True,
                           event_text: str | None = None, **meta: Any) -> Job:
        """Queue one turn of a conversation for its owner agent."""
        with db.session() as s:
            thread = s.get(Thread, thread_id)
        if thread is None:
            raise LookupError(f"thread {thread_id} not found")
        owner = load_agent(thread.agent_id)
        if from_user:
            await self.save_message(thread_id, Role.user, prompt)
        elif event_text:
            await self.save_message(thread_id, Role.event, event_text, **meta)
        job = Job(kind=JobKind.turn, origin_thread_id=thread_id, assignee_agent_id=owner.id,
                  task=prompt, depth=0, chain=[owner.id], pool=owner.pool,
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
        """Run a job delivered to a worker. Safe to call again for a redelivered job."""
        job = load_job(job_id)
        if job is None or job.status in (JobStatus.done, JobStatus.failed):
            return  # unknown, or already finished before a crash prevented the ack
        attempts = job.attempts + 1
        if attempts > settings.max_attempts:
            await self._give_up(job)
            return
        job = update_job(job.id, status=JobStatus.running, attempts=attempts, worker=worker,
                         started_at=_now())
        if attempts > 1:
            await self.broker.publish(job.origin_thread_id, "job_retry", job_id=job.id,
                                      attempt=attempts, worker=worker)
        assignee = load_agent(job.assignee_agent_id)
        with job_span(job, assignee.name, worker) as span:
            if job.kind == JobKind.turn:
                await self._run_turn(job)
            else:
                await self._run_task(job)
            set_output(span, (load_job(job.id).result or {}).get("reply")
                       or load_job(job.id).result)

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
        deps = RunDeps(thread_id=thread_id, agent_id=agent.id, depth=0, chain=[agent.id],
                       job_id=state.get("job_id"))
        reply, messages = await self.run_agent(agent, state["prompt"], deps, history=history)
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
                {"agent_id": job.assignee_agent_id, "prompt": job.task, "job_id": job.id},
                config={"configurable": {"thread_id": thread_id}})
            reply = out["reply"]
            await self.save_message(thread_id, Role.agent, reply, agent_id=job.assignee_agent_id)
            update_job(job.id, status=JobStatus.done, result={"reply": reply}, finished_at=_now())
            if (job.meta or {}).get("kind") == "mqtt":
                await self._capture_event(job, reply)
        except Exception as e:
            log.exception("turn %s failed", job.id)
            await self.save_message(thread_id, Role.event, f"Error: {e}", kind="error")
            update_job(job.id, status=JobStatus.failed, error=str(e), finished_at=_now())
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
                       chain=list(job.chain), job_id=job.id)
        await self.broker.publish(job.origin_thread_id, "delegation_started", job_id=job.id,
                                  from_agent=requester.name if requester else None,
                                  to_agent=assignee.name, task=job.task,
                                  background=job.background, depth=job.depth,
                                  worker=job.worker)
        try:
            result, _ = await self.run_agent(assignee, job.task, deps, as_worker=True)
            update_job(job.id, status=JobStatus.done, result=result.model_dump(),
                       finished_at=_now())
        except Exception as e:
            log.exception("task %s failed", job.id)
            result = WorkResult(status="failed", summary=f"{assignee.name} hit an error: {e}")
            update_job(job.id, status=JobStatus.failed, error=str(e),
                       result=result.model_dump(), finished_at=_now())
        await self._finish_task(job, result)

    async def _finish_task(self, job: Job, result: WorkResult) -> None:
        assignee = load_agent(job.assignee_agent_id)
        requester = load_agent(job.requester_agent_id) if job.requester_agent_id else None
        await self.broker.publish(job.origin_thread_id, "delegation_finished", job_id=job.id,
                                  from_agent=requester.name if requester else None,
                                  to_agent=assignee.name, result=result.model_dump(),
                                  depth=job.depth)
        await self.save_message(
            job.origin_thread_id, Role.event,
            f"{assignee.name} → {requester.name if requester else 'you'}: "
            f"[{result.status}] {result.summary}",
            agent_id=assignee.id, kind="delegation_result", job_id=job.id,
            result=result.model_dump())
        await self._capture_delegation(job, assignee, result)
        # Wake a waiting delegator, if there is one.
        await self.broker.push_result(job.id, result.model_dump())
        # Background jobs (including waits that timed out) report into the origin thread.
        if load_job(job.id).background:
            await self.enqueue_turn(job.origin_thread_id, self._report_prompt(job, assignee, result),
                                    from_user=False)

    @staticmethod
    def _report_prompt(job: Job, assignee: AgentDef, result: WorkResult) -> str:
        return (f"[Background job {job.id} finished]\n"
                f"{assignee.name} was asked: {job.task}\n"
                f"Result: {result.model_dump_json()}\n\n"
                f"Report this outcome to the user now as {assignee.name}'s work: say you had "
                f"asked {assignee.name} and what they reported, in your own words. Do not start "
                "new work unless the result requires it.")

    # ------------------------------------------------------------ agents

    def build_agent(self, agent: AgentDef, *, as_worker: bool, on_fallback: bool = False,
                    memory_context: str = "") -> Agent:
        instructions = [agent.instructions or f"You are {agent.name}."]
        roster = team_roster(agent)
        if roster:
            instructions.append(roster)
        if as_worker:
            instructions.append(
                "You are working on a task delegated by another agent. Do the work, then "
                "return a WorkResult. If you cannot finish, return status 'failed' or "
                "'partial' and explain why in the summary.")
        toolsets = build_tool_toolsets(agent)
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
        model = (build_model_for(agent.fallback_provider_id, agent.fallback_model,
                                 f"{agent.name} (fallback)")
                 if on_fallback else build_agent_model(agent))
        return Agent(
            model,
            name=agent.name,
            instructions="\n\n".join(instructions),
            deps_type=RunDeps,
            output_type=WorkResult if as_worker else str,
            toolsets=[ObservedToolset(t, agent_name=agent.name, broker=self.broker,
                                      desktop_server_id=desk) for t, desk in toolsets],
        )

    async def run_agent(self, agent: AgentDef, prompt: str, deps: RunDeps, *,
                        history: list | None = None, as_worker: bool = False):
        """Run on the agent's model; on failure or escalation, rerun on its fallback model."""
        recalled = await self._recall(agent, prompt, deps)
        servers = agent_tool_servers(agent)
        await self.broker.publish(deps.thread_id, "agent_tools", agent=agent.name, agent_id=agent.id,
                                  servers=[s.name for s in servers], depth=deps.depth)
        try:
            pa_agent = self.build_agent(agent, as_worker=as_worker, memory_context=recalled)
            result = await pa_agent.run(prompt, deps=deps, message_history=history,
                                        conversation_id=deps.thread_id)
            return result.output, result.all_messages()
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            if not has_fallback(agent) or not isinstance(e, Exception):
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
            note = (f"\n\n[A smaller model attempted this first and stopped: {reason[:500]}. "
                    "Take over and complete it.]")
            pa_agent = self.build_agent(agent, as_worker=as_worker, on_fallback=True,
                                        memory_context=recalled)
            result = await pa_agent.run(prompt + note, deps=deps, message_history=history,
                                        conversation_id=deps.thread_id)
            return result.output, result.all_messages()

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
        text = (f"Event on MQTT topic {meta.get('topic', '?')}: {str(meta.get('event', ''))[:400]}\n"
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
            async with released_slot():
                got = await rt.broker.wait_handback(srv.id, timeout)
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
                           background: bool = False) -> dict:
            """Hand a task to a team member and get their result.

            Args:
                agent_name: Name of the team member (from the team list in your instructions).
                task: A complete, self-contained description of what to do and what to return.
                background: False (default) waits and returns their result now. True returns a
                    job ID immediately; you will be woken in this conversation with the result.
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

            job = await rt._submit(Job(
                kind=JobKind.task, origin_thread_id=ctx.deps.thread_id, requester_agent_id=me.id,
                assignee_agent_id=target.id, parent_job_id=ctx.deps.job_id, task=task,
                background=background, depth=ctx.deps.depth + 1,
                chain=[*ctx.deps.chain, target.id], pool=target.pool))
            if background:
                return {"status": "queued", "job_id": job.id,
                        "summary": f"{target.name} is working on it in the background. You will "
                        "be woken in this conversation with the result. Tell the user it is underway."}

            async with released_slot():
                result = await rt.broker.wait_result(job.id, settings.delegate_wait_timeout)
            if result is not None:
                return attributed(target.name, result)
            # Taking too long: convert to a background job so the result still gets reported.
            job = update_job(job.id, background=True)
            if job.status in (JobStatus.done, JobStatus.failed) and job.result:
                return attributed(target.name, job.result)
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
