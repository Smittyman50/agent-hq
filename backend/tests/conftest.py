"""Test harness.

* A real redis-server (Valkey-compatible) is started for the session.
* Workers run in-process, against the same Redis, with scripted fake models.
* Postgres tests start a throwaway cluster when the binaries are available.

The fake "boss" model reads the team roster out of its instructions (exactly as a
real model would) and delegates to whoever is listed first, so these tests also
prove nothing about agent names is hard-coded.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlmodel import select

from app import db
from app.broker import Broker
from app.config import reload_settings, settings
from app.llm import register_model_factory
from app.main import create_app
from app.models import Job, JobStatus, Message, Role
from app.runtime import Runtime
from app.worker import Worker

# --------------------------------------------------------------------------- fake models


def _instructions(messages: list[ModelMessage]) -> str:
    for m in reversed(messages):
        if getattr(m, "instructions", None):
            return m.instructions
    return ""


def _last_prompt(messages: list[ModelMessage]) -> str:
    for m in reversed(messages):
        for p in getattr(m, "parts", []):
            if p.part_kind == "user-prompt":
                return p.content if isinstance(p.content, str) else str(p.content)
    return ""


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    """Tool returns since the latest user prompt."""
    out: list[ToolReturnPart] = []
    for m in reversed(messages):
        parts = getattr(m, "parts", [])
        if any(p.part_kind == "user-prompt" for p in parts):
            break
        out.extend(p for p in parts if isinstance(p, ToolReturnPart))
    return out


def boss(background: bool):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = _last_prompt(messages)
        if prompt.startswith("[Background job"):
            result = json.loads(prompt.split("Result: ", 1)[1].split("\n", 1)[0])
            return ModelResponse(parts=[TextPart(f"Update for you: {result['summary']}")])
        returns = _tool_returns(messages)
        if returns:
            r = returns[-1].content
            return ModelResponse(parts=[TextPart(f"Report: {r['summary']}")])
        roster = _instructions(messages).partition("Team members you can delegate to:")[2]
        team = re.findall(r"^- ([^:]+):", roster, re.M)
        if not team:
            n = sum(1 for m in messages if getattr(m, "kind", "") == "request")
            return ModelResponse(parts=[TextPart(f"I have no team. (turn {n}: {prompt[:60]})")])
        return ModelResponse(parts=[ToolCallPart("delegate", {
            "agent_name": team[0], "task": f"Please handle: {prompt}", "background": background})])
    return fn


def worker_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = _tool_returns(messages)
    tools = {t.name for t in info.function_tools}
    if "demo_add" in tools and not returns:
        return ModelResponse(parts=[ToolCallPart("demo_add", {"a": 2, "b": 3})])
    extra = f" (tool said {returns[-1].content})" if returns else ""
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "status": "done", "summary": f"Worker finished{extra}", "details": _last_prompt(messages)})])


def broken(messages, info):
    raise RuntimeError("model server unreachable")


HANG = {"calls": 0}


async def hang_once(messages, info):
    """First call hangs (the worker 'dies' mid-job); later calls succeed."""
    HANG["calls"] += 1
    if HANG["calls"] == 1:
        await asyncio.sleep(3600)
    return worker_fn(messages, info)


async def slow_worker(messages, info):
    """Takes longer than redis-py's default 5 s socket timeout (a real browsing task)."""
    if not _tool_returns(messages):
        await asyncio.sleep(7)
    return worker_fn(messages, info)


def alert(messages, info):
    return ModelResponse(parts=[TextPart("Handled event: " + _last_prompt(messages).split("\n\n")[1])])


def spawner(spawns: int = 1, role: str = "Feed reader"):
    """Uses spawn_subagent `spawns` times (if it has the tool), then reports every result."""
    def fn(messages, info):
        tools = {t.name for t in info.function_tools}
        prompt = _last_prompt(messages)
        returns = [r for r in reversed(_tool_returns(messages)) if r.tool_name == "spawn_subagent"]
        if "spawn_subagent" not in tools:
            return ModelResponse(parts=[TextPart("I can't create sub-agents.")])
        if prompt.startswith("[Sub-agent request") and "denied" in prompt:
            return ModelResponse(parts=[TextPart("Understood, I'll do it myself.")])
        if len(returns) < spawns:
            return ModelResponse(parts=[ToolCallPart("spawn_subagent", {
                "role": role, "task": f"Read part {len(returns) + 1} of: {prompt[:80]}",
                "why_not_existing_agents": "Nobody on the team reads feeds in parallel."})])
        return ModelResponse(parts=[TextPart(" | ".join(
            f"{r.content.get('status')}: {r.content.get('summary')}" for r in returns))])
    return fn


def requester(messages, info):
    """Asks for sub-agents; once approved, spawns one; reports what happened."""
    prompt = _last_prompt(messages)
    returns = _tool_returns(messages)
    if prompt.startswith("[Sub-agent request"):
        return spawner(1)(messages, info)
    if not returns:
        return ModelResponse(parts=[ToolCallPart("request_subagents", {
            "why": "Twelve feeds to read before the morning brief; in parallel it's faster.",
            "why_not_existing_agents": "No existing agent reads feeds; this is a one-off.",
            "subagents": [{"role": "Feed reader", "purpose": "Read six feeds and list headlines",
                           "est_minutes": 5}], "hours": 2})])
    return ModelResponse(parts=[TextPart(f"Request: {returns[-1].content['status']}")])


RETRY = {"calls": 0}


def retrying(messages, info):
    """Calls a tool that doesn't exist first (costs a second model request)."""
    if not _tool_returns(messages) and not any(
            p.part_kind == "retry-prompt" for m in messages for p in getattr(m, "parts", [])):
        return ModelResponse(parts=[ToolCallPart("no_such_tool", {})])
    return worker_fn(messages, info)


def coder(calls: int = 1):
    """Runs print(6*7) on its workbench `calls` times, then reports what came back."""
    def fn(messages, info):
        tool = next((t.name for t in info.function_tools if t.name.endswith("run_python")), None)
        returns = [r for r in reversed(_tool_returns(messages)) if r.tool_name == tool]
        if tool is None:
            return ModelResponse(parts=[TextPart("I have no workbench.")])
        if len(returns) < calls:
            return ModelResponse(parts=[ToolCallPart(tool, {"code": "print(6*7)"})])
        def show(c):
            return c.get("stdout", "").strip() if isinstance(c, dict) else str(c)
        return ModelResponse(parts=[TextPart(" | ".join(show(r.content) for r in returns))])
    return fn


SCRIPTS = {"boss": boss(False), "boss-bg": boss(True), "worker": worker_fn, "broken": broken,
           "hang-once": hang_once, "alert": alert, "slow-worker": slow_worker,
           "spawner": spawner(1), "spawner-2": spawner(2), "spawn-alpha": spawner(1, "Alpha"),
           "requester": requester, "retrying": retrying,
           "coder": coder(1), "coder-2": coder(2)}


@pytest.fixture(autouse=True)
def fake_models():
    HANG["calls"] = 0
    register_model_factory("fake", lambda name: FunctionModel(SCRIPTS[name], model_name=name))


# --------------------------------------------------------------------------- services


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_url():
    exe = shutil.which("valkey-server") or shutil.which("redis-server")
    if not exe:
        pytest.skip("valkey-server/redis-server not installed")
    port = _free_port()
    proc = subprocess.Popen([exe, "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    yield f"redis://127.0.0.1:{port}/0"
    proc.terminate()
    proc.wait()


PG_BIN = Path("/usr/lib/postgresql/16/bin")


@pytest.fixture(scope="session")
def postgres_url():
    if not (PG_BIN / "initdb").exists():
        pytest.skip("Postgres binaries not available")
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="ahq-pg-"))
    root.chmod(0o777)  # the cluster runs as the postgres user when tests run as root
    data = root / "data"
    port = _free_port()
    as_pg = ["runuser", "-u", "postgres", "--"] if os.geteuid() == 0 else []
    subprocess.run([*as_pg, str(PG_BIN / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8", "--locale=C.UTF-8"],
                   check=True, capture_output=True)
    subprocess.run([*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-o",
                    f"-p {port} -k /tmp -c listen_addresses=127.0.0.1", "-w", "-l", str(root / "pg.log"), "start"],
                   check=True, capture_output=True)
    subprocess.run([*as_pg, str(PG_BIN / "createdb"), "-h", "127.0.0.1", "-p", str(port),
                    "-U", "postgres", "agent_hq"], check=True, capture_output=True)
    yield f"postgresql://postgres@127.0.0.1:{port}/agent_hq"
    subprocess.run([*as_pg, str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
                   capture_output=True)
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
async def hq(tmp_path, monkeypatch, redis_url):
    """API app + runtime against fresh Redis and SQLite. Use hq.start_worker() to add workers."""
    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AHQ_REDIS_URL", redis_url)
    monkeypatch.setenv("AHQ_DB_URL", os.environ.get("AHQ_TEST_DB_URL", ""))
    reload_settings()
    db.reset_engine()
    db.init_db()
    h = Harness(redis_url)
    await h.broker.r.flushdb()
    yield h
    await h.close()


class Harness:
    def __init__(self, redis_url: str) -> None:
        self.broker = Broker(redis_url)
        self.saver = InMemorySaver()  # shared by all in-process workers, like Postgres would be
        self.runtime = Runtime(self.broker)
        self.app = create_app(lifespan_=None)
        self.app.state.broker = self.broker
        self.app.state.runtime = self.runtime
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://test")
        self.sign_in()
        self.anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                      base_url="http://test")
        self.workers: list[Worker] = []

    def sign_in(self) -> None:
        """Every API call needs a sign-in: the tests use an API token for an account
        "tester". Call again after switching to another database."""
        from app import auth
        from app.models import User

        with db.session() as s:
            user = s.exec(select(User).where(User.username == "tester")).first()
            if user is None:
                user = User(username="tester", password_hash=auth.hash_password("correct horse battery"))
                s.add(user)
                s.commit()
                s.refresh(user)
        self.user = user
        _, self.token = auth.create_token(user, "tests", None)
        self.http.headers["Authorization"] = f"Bearer {self.token}"

    async def start_worker(self, name="w1", pools=("default",), concurrency=4,
                           claim_idle_ms=60000, checkpointer=None) -> Worker:
        rt = Runtime(Broker(self.broker.url), checkpointer or self.saver)
        w = Worker(rt, name=name, pools=list(pools), concurrency=concurrency,
                   claim_idle_ms=claim_idle_ms, heartbeat=0.2)
        await w.start()
        self.workers.append(w)
        return w

    async def agent(self, **kw) -> dict:
        r = await self.http.post("/api/agents", json=kw)
        assert r.status_code == 201, r.text
        return r.json()

    async def thread(self, agent_id: str) -> dict:
        r = await self.http.post("/api/threads", json={"agent_id": agent_id})
        assert r.status_code == 201, r.text
        return r.json()

    async def say(self, thread_id: str, text: str) -> None:
        r = await self.http.post(f"/api/threads/{thread_id}/messages", json={"content": text})
        assert r.status_code == 202, r.text

    def messages(self, thread_id: str) -> list[Message]:
        with db.session() as s:
            return list(s.exec(select(Message).where(Message.thread_id == thread_id)
                               .order_by(Message.created_at)).all())

    def jobs(self) -> list[Job]:
        with db.session() as s:
            return list(s.exec(select(Job).order_by(Job.created_at)).all())

    async def wait_agent_replies(self, thread_id: str, n: int, timeout: float = 15) -> list[str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            replies = [m.content for m in self.messages(thread_id) if m.role == Role.agent]
            if len(replies) >= n and all(j.status in (JobStatus.done, JobStatus.failed)
                                         for j in self.jobs()):
                return replies
            await asyncio.sleep(0.05)
        raise AssertionError(f"timed out; messages={[(m.role, m.content) for m in self.messages(thread_id)]}"
                             f" jobs={[(j.kind, j.status, j.attempts) for j in self.jobs()]}")

    async def close(self) -> None:
        for w in self.workers:
            await w.stop(graceful=False)
            await w.broker.close()
        await self.http.aclose()
        await self.anon.aclose()
        await self.broker.close()
