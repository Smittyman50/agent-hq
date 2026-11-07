"""Model providers: CRUD, write-only keys, model listing, and agents on two different
Ollama models running end to end through a worker against a fake Ollama server."""

from __future__ import annotations

import socket
import threading
import time

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

from app.llm import build_provider_model
from app.models import ModelProvider, ProviderKind

CHAT_REQUESTS: list[dict] = []


def fake_provider_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/tags")  # Ollama native model list
    def tags():
        return {"models": [
            {"name": "qwen3:14b", "size": 9_300_000_000,
             "details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}},
            {"name": "gpt-oss:20b", "size": 13_800_000_000,
             "details": {"parameter_size": "20.9B", "quantization_level": "MXFP4"}}]}

    @app.post("/v1/chat/completions")  # Ollama's / any OpenAI-compatible chat endpoint
    async def chat(request: Request):
        body = await request.json()
        CHAT_REQUESTS.append(body)
        return {"id": "c1", "object": "chat.completion", "created": 0, "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": f"answered by {body['model']}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    @app.get("/compat/v1/models")  # OpenAI-compatible model list
    def compat_models(authorization: str | None = Header(None)):
        if authorization != "Bearer sk-local":
            raise HTTPException(401)
        return {"data": [{"id": "llama-3.3-70b", "owned_by": "vllm"}]}

    @app.get("/anthropic/v1/models")
    def anthropic_models(x_api_key: str | None = Header(None)):
        if x_api_key != "sk-ant-test":
            raise HTTPException(401)
        return {"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]}

    return app


@pytest.fixture(scope="module")
def fake_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(fake_provider_app(), host="127.0.0.1", port=port,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(5)


async def test_provider_crud_keeps_key_write_only(hq):
    r = await hq.http.post("/api/providers", json={
        "name": "OpenAI", "kind": "openai", "api_key": "sk-secret"})
    assert r.status_code == 201
    p = r.json()
    assert p["has_api_key"] is True and "api_key" not in p
    # Updating without api_key keeps it; "" clears it.
    p2 = (await hq.http.put(f"/api/providers/{p['id']}", json={"name": "OpenAI", "kind": "openai"})).json()
    assert p2["has_api_key"] is True
    p3 = (await hq.http.put(f"/api/providers/{p['id']}",
                            json={"name": "OpenAI", "kind": "openai", "api_key": ""})).json()
    assert p3["has_api_key"] is False
    bad = await hq.http.post("/api/providers", json={"name": "vllm", "kind": "openai_compatible"})
    assert bad.status_code == 422


async def test_lists_models_for_each_kind(hq, fake_server):
    async def models(**kw):
        p = (await hq.http.post("/api/providers", json=kw)).json()
        r = await hq.http.get(f"/api/providers/{p['id']}/models")
        assert r.status_code == 200, r.text
        return r.json()

    ollama = await models(name="Ollama GPU", kind="ollama", base_url=fake_server)
    assert [m["id"] for m in ollama] == ["gpt-oss:20b", "qwen3:14b"]
    assert ollama[1]["details"] == "14.8B · Q4_K_M · 9.3 GB"
    # The /v1 form of an Ollama URL works too.
    assert len(await models(name="Ollama v1", kind="ollama", base_url=fake_server + "/v1")) == 2
    compat = await models(name="vLLM", kind="openai_compatible",
                          base_url=fake_server + "/compat/v1", api_key="sk-local")
    assert compat == [{"id": "llama-3.3-70b", "details": "vllm"}]
    anth = await models(name="Anthropic", kind="anthropic", base_url=fake_server + "/anthropic",
                        api_key="sk-ant-test")
    assert anth[0]["id"] == "claude-sonnet-4-5"


async def test_unreachable_provider_reports_502(hq):
    p = (await hq.http.post("/api/providers", json={
        "name": "Down", "kind": "ollama", "base_url": "http://127.0.0.1:9"})).json()
    r = await hq.http.get(f"/api/providers/{p['id']}/models")
    assert r.status_code == 502 and "Down" in r.json()["detail"]


def test_builds_the_right_model_objects():
    m = build_provider_model(ModelProvider(name="o", kind=ProviderKind.ollama,
                                           base_url="http://gpu:11434"), "qwen3:14b")
    assert m.model_name == "qwen3:14b" and m.base_url.rstrip("/") == "http://gpu:11434/v1"
    m = build_provider_model(ModelProvider(name="c", kind=ProviderKind.openai_compatible,
                                           base_url="http://vllm:8000/v1"), "llama")
    assert m.base_url.rstrip("/") == "http://vllm:8000/v1"
    m = build_provider_model(ModelProvider(name="a", kind=ProviderKind.anthropic, api_key="k"),
                             "claude-sonnet-4-5")
    assert m.model_name == "claude-sonnet-4-5"
    with pytest.raises(ValueError):
        build_provider_model(ModelProvider(name="o", kind=ProviderKind.ollama), "")


async def test_two_agents_on_two_ollama_models_end_to_end(hq, fake_server):
    CHAT_REQUESTS.clear()
    await hq.start_worker()
    p = (await hq.http.post("/api/providers", json={
        "name": "Ollama GPU", "kind": "ollama", "base_url": fake_server})).json()
    replies = {}
    for name, model in (("Qwen agent", "qwen3:14b"), ("GPT-OSS agent", "gpt-oss:20b")):
        a = await hq.agent(name=name, provider_id=p["id"], model=model)
        t = await hq.thread(a["id"])
        await hq.say(t["id"], "hello")
        replies[model] = (await hq.wait_agent_replies(t["id"], 1))[0]
    assert replies == {"qwen3:14b": "answered by qwen3:14b", "gpt-oss:20b": "answered by gpt-oss:20b"}
    assert sorted(r["model"] for r in CHAT_REQUESTS) == ["gpt-oss:20b", "qwen3:14b"]


async def test_provider_in_use_cannot_be_deleted(hq):
    p = (await hq.http.post("/api/providers", json={"name": "Ollama", "kind": "ollama"})).json()
    await hq.agent(name="Uses it", provider_id=p["id"], model="qwen3:14b")
    r = await hq.http.delete(f"/api/providers/{p['id']}")
    assert r.status_code == 409 and "Uses it" in r.json()["detail"]


async def test_disabled_provider_is_reported_in_the_thread(hq):
    await hq.start_worker()
    p = (await hq.http.post("/api/providers", json={
        "name": "Off", "kind": "ollama", "enabled": False})).json()
    a = await hq.agent(name="Idle", provider_id=p["id"], model="qwen3:14b")
    t = await hq.thread(a["id"])
    await hq.say(t["id"], "hi")
    import asyncio
    for _ in range(100):
        events = [m.content for m in hq.messages(t["id"]) if m.role.value == "event"]
        if events:
            break
        await asyncio.sleep(0.05)
    assert "model provider 'Off' is disabled" in events[0]
