"""Thinking: how much an agent's model reasons before it answers.

Each agent has a `thinking` setting, chosen in the UI. It is sent as a model setting on
every run, never written into the instructions: telling a model to "think hard" in its
prompt doesn't change how much it actually reasons.

Most providers go through PydanticAI's unified `thinking` model setting, which it
translates per provider (OpenAI and ChatGPT: reasoning effort; Anthropic: thinking
budgets or adaptive thinking). PydanticAI doesn't translate it for Ollama, so for Ollama
models Agent HQ sends `reasoning_effort` itself, which Ollama's OpenAI-compatible
endpoint honours ("none" turns thinking off). It is only sent to Ollama models that
report the "thinking" capability, so models that can't think aren't sent a setting they
would reject.

Models that can't change how much they think keep their default, and say so in the UI.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from pydantic_ai.settings import ModelSettings, ThinkingLevel

from . import db
from .models import AgentDef, ModelProvider, ProviderKind

log = logging.getLogger(__name__)

# Stored value -> PydanticAI's unified level. "" (or None) means: send nothing, so the
# model does what it does by default.
LEVELS: dict[str, ThinkingLevel | None] = {
    "": None,
    "off": False,
    "on": True,
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

# Unified level -> the reasoning_effort Ollama's /v1/chat/completions accepts.
# "on" has no Ollama equivalent of "the provider's default", so it asks for medium.
OLLAMA_EFFORT: dict[Any, str] = {
    False: "none",
    True: "medium",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def level_of(value: str | None) -> ThinkingLevel | None:
    """The unified level for a stored value; None (model default) for empty or unknown."""
    key = (value or "").strip().lower()
    if key not in LEVELS:
        log.warning("unknown thinking level %r: using the model's default", value)
        return None
    return LEVELS[key]


# --------------------------------------------------------------------------- Ollama

_caps: dict[tuple[str, str], tuple[float, bool | None]] = {}


async def ollama_thinks(p: ModelProvider, model: str) -> bool | None:
    """Whether Ollama says the model can think (its "thinking" capability).
    None when Ollama couldn't be asked. Cached for ten minutes."""
    import httpx

    from .llm import ollama_root

    key = (p.id, model)
    hit = _caps.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    found: bool | None = None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.post(f"{ollama_root(p)}/api/show", json={"model": model})
            if r.status_code == 200:
                found = "thinking" in (r.json().get("capabilities") or [])
    except Exception as e:
        log.debug("couldn't ask Ollama about %s: %s", model, e)
    if found is not None:  # don't cache "couldn't ask"
        _caps[key] = (time.monotonic(), found)
    return found


def ollama_has_levels(model: str) -> bool:
    """Whether an Ollama model takes effort levels rather than just on/off. Ollama's
    capabilities don't say, so this goes by family: gpt-oss is the one that does."""
    return "gpt-oss" in model.lower()


# --------------------------------------------------------------------------- settings


def _provider(pid: str | None) -> ModelProvider | None:
    if not pid:
        return None
    with db.session() as s:
        return s.get(ModelProvider, pid)


async def settings_for(agent: AgentDef, *, on_fallback: bool = False) -> ModelSettings | None:
    """The model settings that carry the agent's thinking level to the model it runs on
    (its main model, or its fallback). None: send nothing."""
    level = level_of(agent.thinking)
    if level is None:
        return None
    pid, model = ((agent.fallback_provider_id, agent.fallback_model) if on_fallback
                  else (agent.provider_id, agent.model))
    p = _provider(pid)
    if p is not None and p.kind == ProviderKind.ollama:
        if await ollama_thinks(p, model) is False:
            return None  # it can't think, and Ollama rejects thinking requests to such models
        return ModelSettings(extra_body={"reasoning_effort": OLLAMA_EFFORT[level]})
    return ModelSettings(thinking=level)


async def support(provider_id: str | None, model: str) -> dict[str, Any]:
    """Whether a thinking setting reaches this model, for the hint in the agent settings.
    {"supported": True | False | None (unknown), "levels": whether it takes effort levels
    (False: on/off only; None: unknown), "note": str}"""
    if not model:
        return {"supported": None, "levels": None, "note": ""}
    p = _provider(provider_id)
    if p is not None and p.kind == ProviderKind.ollama:
        thinks = await ollama_thinks(p, model)
        if thinks is None:
            return {"supported": None, "levels": None,
                    "note": "Couldn't ask Ollama whether this model can think."}
        if not thinks:
            return {"supported": False, "levels": False,
                    "note": "Ollama says this model can't think, so this setting is ignored."}
        if ollama_has_levels(model):
            return {"supported": True, "levels": True,
                    "note": "Sent to Ollama as reasoning_effort. This model always thinks a little, "
                            "so Off gives it its lowest effort."}
        return {"supported": True, "levels": False,
                "note": "Sent to Ollama as reasoning_effort."}
    try:
        from .llm import build_model_for

        m = build_model_for(provider_id, model, "this agent")
        profile = m.profile
    except Exception as e:
        return {"supported": None, "levels": None, "note": f"Couldn't check this model: {e}"}
    get = profile.get if isinstance(profile, dict) else lambda k, d=None: getattr(profile, k, d)
    if not get("supports_thinking", False):
        return {"supported": False, "levels": False,
                "note": "PydanticAI doesn't know how to set thinking for this model, so this "
                        "setting is ignored and it keeps its default."}
    if get("thinking_always_enabled", False):
        return {"supported": True, "levels": True,
                "note": "This model always thinks, so Off gives it its lowest effort."}
    return {"supported": True, "levels": True,
            "note": "Levels the model doesn't offer use the closest one it does."}
