"""Model providers: turn an agent's provider + model into a PydanticAI model, and list
the models a provider offers.

Providers are rows in the database, managed on the Models page. Nothing about
specific servers or model names lives in the code.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx
from pydantic_ai.models import Model, infer_model

from . import db
from .models import AgentDef, ModelProvider, ProviderKind

_factories: dict[str, Callable[[str], Model]] = {}

DEFAULT_URLS = {
    ProviderKind.ollama: "http://localhost:11434",
    ProviderKind.openai: "https://api.openai.com/v1",
    ProviderKind.anthropic: "https://api.anthropic.com",
}


def register_model_factory(prefix: str, factory: Callable[[str], Model]) -> None:
    """Add a custom spec prefix for provider-less agents (the tests use this for fakes)."""
    _factories[prefix] = factory


def build_model(spec: str) -> Model:
    """A raw PydanticAI model spec, e.g. "openai:gpt-4.1" or "test"."""
    prefix, _, name = spec.partition(":")
    if prefix in _factories:
        return _factories[prefix](name)
    return infer_model(spec)


def build_model_for(provider_id: str | None, model: str, who: str) -> Model:
    """A provider + model name (or a raw spec when there is no provider)."""
    if not provider_id:
        if not model:
            raise ValueError(f"{who} has no model provider or model selected")
        return build_model(model)
    with db.session() as s:
        provider = s.get(ModelProvider, provider_id)
    if provider is None:
        raise ValueError(f"{who}'s model provider no longer exists")
    if not provider.enabled:
        raise ValueError(f"model provider '{provider.name}' is disabled")
    built = build_provider_model(provider, model)
    if provider.max_concurrent and provider.max_concurrent > 0:
        from .slots import SlotLimitedModel

        return SlotLimitedModel(built, provider.id, provider.max_concurrent)
    return built


def build_agent_model(agent: AgentDef) -> Model:
    return build_model_for(agent.provider_id, agent.model, agent.name)


def describe_model(provider_id: str | None, model: str) -> str:
    if not provider_id:
        return model
    with db.session() as s:
        p = s.get(ModelProvider, provider_id)
    return f"{model} @ {p.name}" if p else model


def ollama_root(p: ModelProvider) -> str:
    """Ollama's base URL without a trailing /v1 (accepts either form)."""
    url = (p.base_url or DEFAULT_URLS[ProviderKind.ollama]).rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def build_provider_model(p: ModelProvider, model_name: str) -> Model:
    if not model_name:
        raise ValueError(f"no model selected for provider '{p.name}'. Open Settings → Agents, "
                         "pick a model from that provider's list and save")
    key = p.api_key or None
    if p.kind == ProviderKind.ollama:
        from pydantic_ai.models.ollama import OllamaModel
        from pydantic_ai.providers.ollama import OllamaProvider

        return OllamaModel(model_name, provider=OllamaProvider(base_url=f"{ollama_root(p)}/v1",
                                                               api_key=key))
    if p.kind in (ProviderKind.openai, ProviderKind.openai_compatible):
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        if p.kind == ProviderKind.openai_compatible:
            if not p.base_url:
                raise ValueError(f"provider '{p.name}' needs a base URL")
            key = key or "not-needed"  # most local servers ignore the key but the client wants one
        return OpenAIChatModel(model_name, provider=OpenAIProvider(base_url=p.base_url or None,
                                                                   api_key=key))
    if p.kind == ProviderKind.anthropic:
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        return AnthropicModel(model_name, provider=AnthropicProvider(api_key=key,
                                                                     base_url=p.base_url or None))
    if p.kind == ProviderKind.chatgpt:
        from . import chatgpt

        return chatgpt.build_model(p, model_name)
    if p.kind == ProviderKind.grok:
        from . import grok

        return grok.build_model(p, model_name)
    raise ValueError(f"unknown provider kind {p.kind}")


async def list_models(p: ModelProvider, timeout: float = 10) -> list[dict]:
    """Ask the provider which models it offers. Returns [{id, details}] sorted by id."""
    if p.kind == ProviderKind.chatgpt:
        from . import chatgpt

        return await chatgpt.list_models(p)  # kept in OpenAI's order
    if p.kind == ProviderKind.grok:
        from . import grok

        return await grok.list_models(p)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if p.kind == ProviderKind.ollama:
            r = await client.get(f"{ollama_root(p)}/api/tags")
            r.raise_for_status()
            out = []
            for m in r.json().get("models", []):
                d = m.get("details") or {}
                bits = [d.get("parameter_size"), d.get("quantization_level")]
                if m.get("size"):
                    bits.append(f"{m['size'] / 1e9:.1f} GB")
                out.append({"id": m["name"], "details": " · ".join(b for b in bits if b)})
        elif p.kind in (ProviderKind.openai, ProviderKind.openai_compatible):
            base = (p.base_url or DEFAULT_URLS[ProviderKind.openai]).rstrip("/")
            key = p.api_key or (os.environ.get("OPENAI_API_KEY") if p.kind == ProviderKind.openai else None)
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            r = await client.get(f"{base}/models", headers=headers)
            r.raise_for_status()
            out = [{"id": m["id"], "details": m.get("owned_by", "")} for m in r.json().get("data", [])]
        elif p.kind == ProviderKind.anthropic:
            base = (p.base_url or DEFAULT_URLS[ProviderKind.anthropic]).rstrip("/")
            key = p.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            r = await client.get(f"{base}/v1/models", params={"limit": 100},
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            r.raise_for_status()
            out = [{"id": m["id"], "details": m.get("display_name", "")} for m in r.json().get("data", [])]
        else:
            raise ValueError(f"unknown provider kind {p.kind}")
    return sorted(out, key=lambda m: m["id"])
