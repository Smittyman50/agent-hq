"""Test harness: scripted fake models stand in for LLMs.

The fake "boss" model reads the team roster out of its instructions (exactly as a
real model would) and delegates to whoever is listed first, so these tests also
prove nothing about agent names is hard-coded.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import db
from app.llm import register_model_factory
from app.main import create_app
from app.runtime import Runtime


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
            return ModelResponse(parts=[TextPart("I have no team.")])
        return ModelResponse(parts=[ToolCallPart("delegate", {
            "agent_name": team[0], "task": f"Please handle: {prompt}", "background": background})])
    return fn


def worker(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = _tool_returns(messages)
    tools = {t.name for t in info.function_tools}
    if "demo_add" in tools and not returns:
        return ModelResponse(parts=[ToolCallPart("demo_add", {"a": 2, "b": 3})])
    extra = f" (tool said {returns[-1].content})" if returns else ""
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {
        "status": "done", "summary": f"Worker finished{extra}", "details": _last_prompt(messages)})])


def broken(messages, info):
    raise RuntimeError("model server unreachable")


SCRIPTS = {"boss": boss(False), "boss-bg": boss(True), "worker": worker, "broken": broken}


@pytest.fixture(autouse=True)
def fake_models():
    register_model_factory("fake", lambda name: FunctionModel(SCRIPTS[name], model_name=name))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    db.reset_engine_for_tests(f"sqlite:///{tmp_path / 't.db'}")
    db.init_db()
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = Runtime(InMemorySaver())
        yield

    with TestClient(create_app(lifespan)) as c:
        yield c


@pytest.fixture
def runtime(client) -> Runtime:
    return client.app.state.runtime


def make_agent(client, **kw) -> dict:
    r = client.post("/api/agents", json=kw)
    assert r.status_code == 201, r.text
    return r.json()
