"""Thinking levels: chosen per agent in the UI, sent as a model setting (never as
instructions), translated for Ollama, and reported as supported or not per model."""

from __future__ import annotations

import asyncio
import http.server
import json
import threading

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app import db, thinking
from app.config import reload_settings
from app.llm import register_model_factory
from app.models import AgentDef, ModelProvider, ProviderKind

from .conftest import _last_prompt

THINKERS = {"qwen3:14b", "gpt-oss:20b"}


class _FakeOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        caps = ["completion", "tools"] + (["thinking"] if body["model"] in THINKERS else [])
        data = json.dumps({"capabilities": caps, "parameters": ""}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def ollama_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Just the database (no Redis): enough for settings_for and support."""
    monkeypatch.setenv("AHQ_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AHQ_DB_URL", "")
    reload_settings()
    db.reset_engine()
    db.init_db()
    thinking._caps.clear()
    yield
    db.reset_engine()


def _provider(kind: ProviderKind, base_url: str | None = None, name: str | None = None) -> str:
    with db.session() as s:
        p = ModelProvider(name=name or kind.value, kind=kind, base_url=base_url, api_key="k")
        s.add(p)
        s.commit()
        return p.id


def test_levels():
    assert thinking.level_of("") is None and thinking.level_of(None) is None
    assert thinking.level_of("off") is False and thinking.level_of("on") is True
    assert thinking.level_of("High") == "high"
    assert thinking.level_of("bogus") is None  # e.g. a hand-edited import: model default


async def test_no_level_sends_nothing(store):
    oa = _provider(ProviderKind.openai)
    assert await thinking.settings_for(AgentDef(name="A", provider_id=oa, model="gpt-5.2")) is None


async def test_openai_and_anthropic_use_the_unified_setting(store):
    oa = _provider(ProviderKind.openai)
    a = AgentDef(name="A", provider_id=oa, model="gpt-5.2", thinking="high")
    assert await thinking.settings_for(a) == {"thinking": "high"}
    off = AgentDef(name="B", provider_id=oa, model="gpt-5.2", thinking="off")
    assert await thinking.settings_for(off) == {"thinking": False}
    raw = AgentDef(name="C", model="anthropic:claude-sonnet-4-5", thinking="on")
    assert await thinking.settings_for(raw) == {"thinking": True}


async def test_ollama_gets_reasoning_effort_only_when_the_model_can_think(store, ollama_url):
    ol = _provider(ProviderKind.ollama, ollama_url)
    for level, effort in (("high", "high"), ("off", "none"), ("on", "medium"),
                          ("minimal", "low"), ("max", "high")):
        a = AgentDef(name="A", provider_id=ol, model="qwen3:14b", thinking=level)
        assert await thinking.settings_for(a) == {"extra_body": {"reasoning_effort": effort}}
    # A model that can't think isn't sent anything (Ollama would reject it).
    plain = AgentDef(name="B", provider_id=ol, model="llama3.1:8b", thinking="high")
    assert await thinking.settings_for(plain) is None


async def test_fallback_uses_its_own_provider(store, ollama_url):
    ol = _provider(ProviderKind.ollama, ollama_url)
    oa = _provider(ProviderKind.openai)
    a = AgentDef(name="A", provider_id=ol, model="gpt-oss:20b", thinking="medium",
                 fallback_provider_id=oa, fallback_model="gpt-5.2")
    assert await thinking.settings_for(a) == {"extra_body": {"reasoning_effort": "medium"}}
    assert await thinking.settings_for(a, on_fallback=True) == {"thinking": "medium"}


async def test_unreachable_ollama_still_sends_the_level(store):
    # Better to try than to silently drop it; the model's error (if any) shows in the chat.
    ol = _provider(ProviderKind.ollama, "http://127.0.0.1:9")
    a = AgentDef(name="A", provider_id=ol, model="qwen3:14b", thinking="low")
    assert await thinking.settings_for(a) == {"extra_body": {"reasoning_effort": "low"}}
    assert (await thinking.support(ol, "qwen3:14b"))["supported"] is None


async def test_support_hint(store, ollama_url):
    ol = _provider(ProviderKind.ollama, ollama_url)
    oa = _provider(ProviderKind.openai)
    qwen = await thinking.support(ol, "qwen3:14b")
    assert qwen["supported"] is True and qwen["levels"] is False  # on/off only
    oss = await thinking.support(ol, "gpt-oss:20b")
    assert oss["supported"] is True and oss["levels"] is True
    no = await thinking.support(ol, "llama3.1:8b")
    assert no["supported"] is False and "can't think" in no["note"]
    gpt = await thinking.support(oa, "gpt-5.2")
    assert gpt["supported"] is True and gpt["levels"] is True
    assert (await thinking.support(oa, "gpt-4.1"))["supported"] is False
    assert (await thinking.support(oa, ""))["supported"] is None


# --------------------------------------------------------------------------- end to end


def _thinker(messages, info: AgentInfo) -> ModelResponse:
    got = info.model_request_parameters.thinking  # where PydanticAI puts the unified level
    return ModelResponse(parts=[TextPart(f"thinking={got} for: {_last_prompt(messages)}")])


async def test_agent_runs_with_its_thinking_level(hq):
    # A fake model that, like OpenAI's reasoning models, accepts a thinking level.
    register_model_factory("thinkfake", lambda name: FunctionModel(
        _thinker, model_name=name, profile={"supports_thinking": True}))
    await hq.start_worker()
    a = await hq.agent(name="Deep", model="thinkfake:x", thinking="high")
    assert a["thinking"] == "high"
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hello")
    for _ in range(100):
        replies = [m for m in hq.messages(t["id"]) if m.role == "assistant"]
        if replies:
            break
        await asyncio.sleep(0.05)
    assert replies and replies[-1].content.startswith("thinking=high")


async def test_api_checks_the_level(hq):
    r = await hq.http.post("/api/agents", json={"name": "X", "model": "fake:echo", "thinking": "turbo"})
    assert r.status_code == 422 and "thinking must be one of" in r.text
    a = await hq.agent(name="Y", model="fake:echo")
    assert a["thinking"] == ""  # model default unless chosen
    r = await hq.http.put(f"/api/agents/{a['id']}", json={**a, "thinking": "low"})
    assert r.status_code == 200 and r.json()["thinking"] == "low"
    r = await hq.http.get("/api/agents/thinking-support", params={"model": ""})
    assert r.status_code == 200 and r.json()["supported"] is None
    # Export/import keeps it.
    exported = (await hq.http.get("/api/agents/export")).json()
    assert {x["name"]: x["thinking"] for x in exported["agents"]}["Y"] == "low"
