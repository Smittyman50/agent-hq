"""The agent runtime.

Agents are rebuilt from their database rows on every run, so edits made in the
web UI (new agents, renamed agents, new instructions, new tools, new delegation
rights) take effect on the next message with no restart.

Delegation contract (the part that fixes the "results never come back" problem):

* An agent delegates by calling the `delegate` tool. In the default (wait) mode
  the tool runs the other agent right away and returns its WorkResult as the
  tool's return value, so the result is always in the delegator's context.
* With background=True the tool returns a job ID at once. Every job records the
  conversation it came from (origin_thread_id). When a background job finishes
  - or fails, or is interrupted by a restart - the runtime posts the result into
  that conversation and wakes the conversation's owner agent to report it to you.
* Workers always return a structured WorkResult, including on failure, so there
  is always something to relay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
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
from sqlmodel import select

from . import db
from .config import settings
from .events import bus
from .llm import build_model
from .models import AgentDef, Job, JobStatus, Message, Role, Thread, ToolServer, Transport

log = logging.getLogger("agent_hq.runtime")


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
    job_id: str | None = None   # set when running as a delegate


GENERIC_RULES = """\
Operating rules:
- You may delegate to the team members listed below with the `delegate` tool. Pick by
  their descriptions. Write the task so it stands on its own: they do not see this chat.
- By default `delegate` waits and returns the team member's result to you. Use
  background=true only for long work; you will be woken in this conversation with the
  result when it finishes.
- You are accountable for delegated work: always report its outcome (including failures)
  back to whoever asked you.
"""


# --------------------------------------------------------------------------- helpers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def load_agent(agent_id: str) -> AgentDef:
    with db.session() as s:
        a = s.get(AgentDef, agent_id)
        if a is None:
            raise LookupError(f"agent {agent_id} not found")
        return a


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


def save_message(thread_id: str, role: Role, content: str, agent_id: str | None = None,
                 **meta: Any) -> Message:
    m = Message(thread_id=thread_id, role=role, content=content, agent_id=agent_id, meta=meta)
    with db.session() as s:
        s.add(m)
        s.commit()
        s.refresh(m)
    bus.publish(thread_id, "message", message=json.loads(m.model_dump_json()))
    return m


def build_tool_toolsets(agent: AgentDef) -> list:
    if not agent.tool_server_ids:
        return []
    with db.session() as s:
        servers = [s.get(ToolServer, i) for i in agent.tool_server_ids]
    out = []
    for srv in servers:
        if srv is None or not srv.enabled:
            continue
        ts = mcp_toolset(srv)
        out.append(PrefixedToolset(ts, srv.name) if srv.prefix_tools else ts)
    return out


def mcp_toolset(srv: ToolServer) -> MCPToolset:
    if srv.transport == Transport.http:
        if not srv.url:
            raise ValueError(f"tool server {srv.name} has no URL")
        return MCPToolset(srv.url, id=srv.name)
    if not srv.command:
        raise ValueError(f"tool server {srv.name} has no command")
    config = {"mcpServers": {srv.name: {"command": srv.command, "args": srv.args, "env": srv.env}}}
    return MCPToolset(config, id=srv.name)


def team_roster(agent: AgentDef) -> str:
    team = allowed_delegates(agent)
    if not team:
        return ""
    lines = [f"- {t.name}: {t.description or '(no description)'}" for t in team]
    return GENERIC_RULES + "\nTeam members you can delegate to:\n" + "\n".join(lines)


@dataclass
class ObservedToolset(WrapperToolset):
    """Publishes every tool call and result to the thread's live event stream."""

    agent_name: str = ""

    async def call_tool(self, name, tool_args, ctx: RunContext[RunDeps], tool):
        bus.publish(ctx.deps.thread_id, "tool_call", agent=self.agent_name,
                    agent_id=ctx.deps.agent_id, tool=name, args=tool_args, depth=ctx.deps.depth)
        try:
            result = await super().call_tool(name, tool_args, ctx, tool)
        except Exception as e:
            bus.publish(ctx.deps.thread_id, "tool_result", agent=self.agent_name,
                        agent_id=ctx.deps.agent_id, tool=name, error=str(e), depth=ctx.deps.depth)
            raise
        bus.publish(ctx.deps.thread_id, "tool_result", agent=self.agent_name,
                    agent_id=ctx.deps.agent_id, tool=name, preview=str(result)[:500],
                    depth=ctx.deps.depth)
        return result


# --------------------------------------------------------------------------- runtime


class TurnState(TypedDict, total=False):
    agent_id: str
    prompt: str
    history: list[dict]
    reply: str


class Runtime:
    """Owns the LangGraph conversation graph, background jobs and per-thread locks."""

    def __init__(self, checkpointer) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._bg: set[asyncio.Task] = set()
        g = StateGraph(TurnState)
        g.add_node("run_agent", self._node_run_agent)
        g.add_edge(START, "run_agent")
        g.add_edge("run_agent", END)
        # The checkpointer persists each thread's model-level history (tool calls included).
        self.graph = g.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------ agent execution

    def build_agent(self, agent: AgentDef, *, as_worker: bool) -> Agent:
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
        if roster:
            toolsets.append(self._delegation_toolset())
        return Agent(
            build_model(agent.model),
            name=agent.name,
            instructions="\n\n".join(instructions),
            deps_type=RunDeps,
            output_type=WorkResult if as_worker else str,
            toolsets=[ObservedToolset(t, agent_name=agent.name) for t in toolsets],
        )

    async def run_agent(self, agent: AgentDef, prompt: str, deps: RunDeps, *,
                        history: list | None = None, as_worker: bool = False):
        pa_agent = self.build_agent(agent, as_worker=as_worker)
        result = await pa_agent.run(prompt, deps=deps, message_history=history)
        return result.output, result.all_messages()

    # ------------------------------------------------------------ conversation turns

    async def _node_run_agent(self, state: TurnState, config) -> TurnState:
        thread_id = config["configurable"]["thread_id"]
        agent = load_agent(state["agent_id"])
        history = ModelMessagesTypeAdapter.validate_python(state.get("history") or [])
        deps = RunDeps(thread_id=thread_id, agent_id=agent.id, depth=0, chain=[agent.id])
        reply, messages = await self.run_agent(agent, state["prompt"], deps, history=history)
        return {"reply": reply,
                "history": ModelMessagesTypeAdapter.dump_python(messages, mode="json")}

    async def run_turn(self, thread_id: str, prompt: str, *, from_user: bool = True) -> str:
        """Run one turn of a thread. Turns in the same thread never overlap."""
        with db.session() as s:
            thread = s.get(Thread, thread_id)
        if thread is None:
            raise LookupError(f"thread {thread_id} not found")
        if from_user:
            save_message(thread_id, Role.user, prompt)
        async with self._locks[thread_id]:
            bus.publish(thread_id, "status", state="thinking", agent_id=thread.agent_id)
            try:
                out = await self.graph.ainvoke(
                    {"agent_id": thread.agent_id, "prompt": prompt},
                    config={"configurable": {"thread_id": thread_id}})
                reply = out["reply"]
                save_message(thread_id, Role.agent, reply, agent_id=thread.agent_id)
                return reply
            except Exception as e:  # surface every failure in the conversation
                log.exception("turn failed")
                save_message(thread_id, Role.event, f"Error: {e}", kind="error")
                raise
            finally:
                bus.publish(thread_id, "status", state="idle", agent_id=thread.agent_id)

    # ------------------------------------------------------------ delegation

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

            job = Job(origin_thread_id=ctx.deps.thread_id, requester_agent_id=me.id,
                      assignee_agent_id=target.id, parent_job_id=ctx.deps.job_id,
                      task=task, background=background)
            with db.session() as s:
                s.add(job)
                s.commit()
                s.refresh(job)
            child = RunDeps(thread_id=ctx.deps.thread_id, agent_id=target.id,
                            depth=ctx.deps.depth + 1, chain=[*ctx.deps.chain, target.id],
                            job_id=job.id)
            if background:
                rt._spawn(rt._run_background(job.id, child))
                return {"status": "queued", "job_id": job.id,
                        "summary": f"{target.name} is working on it in the background. You will "
                        "be woken in this conversation with the result. Tell the user it is underway."}
            result = await rt.execute_job(job.id, child)
            return result.model_dump()

        async def job_status(ctx: RunContext[RunDeps], job_id: str) -> dict:
            """Check on a background job you started."""
            with db.session() as s:
                job = s.get(Job, job_id)
            if job is None or job.origin_thread_id != ctx.deps.thread_id:
                return {"error": "no such job in this conversation"}
            return {"status": job.status, "result": job.result, "error": job.error}

        return FunctionToolset([delegate, job_status], id="delegation")

    async def execute_job(self, job_id: str, deps: RunDeps) -> WorkResult:
        """Run a delegated job. Never raises: failures come back as a WorkResult."""
        with db.session() as s:
            job = s.get(Job, job_id)
            job.status = JobStatus.running
            s.add(job)
            s.commit()
            s.refresh(job)
        requester, assignee = load_agent(job.requester_agent_id), load_agent(job.assignee_agent_id)
        bus.publish(job.origin_thread_id, "delegation_started", job_id=job.id,
                    from_agent=requester.name, to_agent=assignee.name, task=job.task,
                    background=job.background, depth=deps.depth)
        try:
            result, _ = await self.run_agent(assignee, job.task, deps, as_worker=True)
            status, error = JobStatus.done, None
        except Exception as e:
            log.exception("job %s failed", job_id)
            result = WorkResult(status="failed", summary=f"{assignee.name} hit an error: {e}")
            status, error = JobStatus.failed, str(e)
        with db.session() as s:
            job = s.get(Job, job_id)
            job.status, job.error = status, error
            job.result = result.model_dump()
            job.finished_at = _now()
            s.add(job)
            s.commit()
        bus.publish(job.origin_thread_id, "delegation_finished", job_id=job_id,
                    from_agent=requester.name, to_agent=assignee.name,
                    result=result.model_dump(), depth=deps.depth)
        save_message(job.origin_thread_id, Role.event,
                     f"{assignee.name} → {requester.name}: [{result.status}] {result.summary}",
                     agent_id=assignee.id, kind="delegation_result", job_id=job_id,
                     result=result.model_dump())
        return result

    async def _run_background(self, job_id: str, deps: RunDeps) -> None:
        result = await self.execute_job(job_id, deps)
        await self.report_job(job_id, result)

    async def report_job(self, job_id: str, result: WorkResult) -> None:
        """Wake the conversation's owner agent in the origin thread to report a job."""
        with db.session() as s:
            job = s.get(Job, job_id)
        assignee = load_agent(job.assignee_agent_id)
        prompt = (
            f"[Background job {job.id} finished]\n"
            f"{assignee.name} was asked: {job.task}\n"
            f"Result: {result.model_dump_json()}\n\n"
            "Report this outcome to the user now. Do not start new work unless the result "
            "requires it.")
        try:
            await self.run_turn(job.origin_thread_id, prompt, from_user=False)
        except Exception:
            log.exception("could not report job %s", job_id)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def recover_interrupted_jobs(self) -> None:
        """On startup, report background jobs that a restart killed, so nothing goes silent."""
        with db.session() as s:
            stuck = s.exec(select(Job).where(Job.status.in_([JobStatus.queued, JobStatus.running]))).all()
            for job in stuck:
                job.status, job.error, job.finished_at = JobStatus.failed, "interrupted by restart", _now()
                job.result = WorkResult(status="failed", summary="Interrupted by a server restart "
                                        "before it finished.").model_dump()
                s.add(job)
            s.commit()
        for job in stuck:
            if job.background:
                self._spawn(self.report_job(job.id, WorkResult(**job.result)))

    async def wait_for_background(self) -> None:
        """Test helper: wait until all background jobs and their reports are done."""
        while self._bg:
            await asyncio.gather(*list(self._bg), return_exceptions=True)
