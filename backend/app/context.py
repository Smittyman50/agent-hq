"""Long conversations: keeping each model call inside the agent's context window.

A model only sees as much as its context window holds, and Ollama silently drops whatever
doesn't fit. Ollama's window is set on the server (OLLAMA_CONTEXT_LENGTH, a Modelfile's
num_ctx, or a default from the GPU's memory: 4K, 32K or 256K); its OpenAI-compatible endpoint,
which Agent HQ uses, can't change it per request. So Agent HQ works out each agent's window
and fits the conversation into it:

* **Compaction, between turns.** When the history plus the agent's instructions would take
  more than a set share of the window, the older turns are summarised (by the agent's own
  model, or one you pick) and replaced by that summary; the most recent turns stay word for
  word. The conversation shows a note with the summary.
* **Trimming, within a turn.** During a long run of tool calls, older tool results are
  shortened, harder as the run approaches the window.
* **A warning** when an agent's instructions alone (its prompt, your profile, skills,
  memories, knowledge excerpts) leave too little room, which means Ollama is already
  cutting them.

The window: the agent's own setting (Settings → Agents), else what the provider reports
(Ollama: the loaded model's context, else the Modelfile's num_ctx; vLLM: max_model_len),
else a default (4,096 for Ollama, the smallest it picks; 128K for cloud providers).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from . import db
from .models import AgentDef, ModelProvider, ProviderKind, SystemSetting

log = logging.getLogger("agent_hq.context")

DEFAULTS: dict[str, Any] = {
    "compaction": True,
    "trigger_ratio": 0.75,       # compact when a call would use more than this share of the window
    "keep_ratio": 0.4,           # share of the history budget kept word for word (recent turns)
    "summary_max_tokens": 1200,  # how long a summary may be
    "reserve_tokens": 2000,      # room for tool definitions and the reply
    "local_window": 4096,        # Ollama window when the server can't tell us
    "cloud_window": 128000,      # cloud providers' window when not set
    "provider_id": None,         # summary model (none = the agent's own model)
    "model": "",
    "warn_overflow": True,
}

SUMMARY_PREFIX = "[Summary of our earlier conversation, written to save space. It replaces the older messages.]\n"
SUMMARY_ACK = "Understood. I have the summary of our earlier conversation."
DROPPED_NOTE = ("[Earlier messages in this conversation were removed to fit the model's context "
                "window; a summary couldn't be written. Ask the user if you need something from them.]")


def get_settings() -> dict[str, Any]:
    with db.session() as s:
        row = s.get(SystemSetting, "conversations")
    return {**DEFAULTS, **(row.value if row else {})}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    merged = {**get_settings(), **{k: v for k, v in values.items() if k in DEFAULTS}}
    merged["trigger_ratio"] = min(0.95, max(0.3, float(merged["trigger_ratio"])))
    merged["keep_ratio"] = min(0.8, max(0.1, float(merged["keep_ratio"])))
    for k, lo, hi in (("summary_max_tokens", 200, 8000), ("reserve_tokens", 0, 32000),
                      ("local_window", 1024, 1_048_576), ("cloud_window", 4096, 4_000_000)):
        merged[k] = min(hi, max(lo, int(merged[k])))
    with db.session() as s:
        row = s.get(SystemSetting, "conversations") or SystemSetting(key="conversations")
        row.value = merged
        s.add(row)
        s.commit()
    return merged


# --------------------------------------------------------------------------- sizes


def tokens(text: str) -> int:
    """A rough token count (about 3.5 characters a token; errs on the high side)."""
    return int(len(text) / 3.5) + 1


def _json_default(o: Any) -> str:
    # Images count as a fixed-size placeholder, not as their bytes.
    if getattr(o, "kind", None) == "binary":
        return "[image]" * 200  # roughly what an image costs a model (~1,500 tokens)
    return str(o)


def _part_text(p: Any) -> str:
    if isinstance(p, (UserPromptPart, TextPart)):
        c = p.content
        return c if isinstance(c, str) else json.dumps(c, default=_json_default)
    if isinstance(p, ToolCallPart):
        return f"{p.tool_name}({json.dumps(p.args, default=str) if not isinstance(p.args, str) else p.args})"
    if isinstance(p, ToolReturnPart):
        c = p.content
        return c if isinstance(c, str) else json.dumps(c, default=_json_default)
    return str(getattr(p, "content", ""))


def history_tokens(messages: list[ModelMessage]) -> int:
    return sum(tokens(_part_text(p)) + 4 for m in messages for p in m.parts)


def turn_starts(messages: list[ModelMessage]) -> list[int]:
    """Where each turn begins: a request carrying the user's prompt. Cutting there never
    separates a tool call from its result."""
    return [i for i, m in enumerate(messages)
            if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)]


def is_summary(messages: list[ModelMessage]) -> bool:
    return bool(messages) and isinstance(messages[0], ModelRequest) and any(
        isinstance(p, UserPromptPart) and isinstance(p.content, str) and p.content.startswith(SUMMARY_PREFIX)
        for p in messages[0].parts)


# --------------------------------------------------------------------------- the window

_cache: dict[tuple[str, str], tuple[float, int | None, str]] = {}


async def detect_window(p: ModelProvider, model: str) -> tuple[int | None, str]:
    """What the provider says the model's context window is, if it says."""
    import httpx

    from .llm import ollama_root

    key = (p.id, model)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1], hit[2]
    found, source = None, ""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            if p.kind == ProviderKind.ollama:
                root = ollama_root(p)
                r = await c.get(f"{root}/api/ps")
                if r.status_code == 200:
                    for m in r.json().get("models") or []:
                        if model in (m.get("name"), m.get("model")) and m.get("context_length"):
                            found, source = int(m["context_length"]), "Ollama (the loaded model)"
                if found is None:
                    r = await c.post(f"{root}/api/show", json={"model": model})
                    if r.status_code == 200:
                        params = r.json().get("parameters") or ""
                        m = re.search(r"^\s*num_ctx\s+(\d+)", params, re.M)
                        if m:
                            found, source = int(m.group(1)), "Ollama (the model's num_ctx)"
            elif p.kind == ProviderKind.openai_compatible and p.base_url:
                r = await c.get(p.base_url.rstrip("/") + "/models",
                                headers={"Authorization": f"Bearer {p.api_key}"} if p.api_key else {})
                if r.status_code == 200:
                    for m in r.json().get("data") or []:
                        if m.get("id") == model and (m.get("max_model_len") or m.get("context_length")):
                            found = int(m.get("max_model_len") or m.get("context_length"))
                            source = "the server's model list"
    except Exception as e:
        log.debug("context window detection failed for %s: %s", model, e)
    _cache[key] = (time.monotonic(), found, source)
    return found, source


async def window_for(agent: AgentDef, *, on_fallback: bool = False) -> tuple[int, str]:
    """The agent's context window in tokens, and where the number came from."""
    if agent.context_window and not on_fallback:
        return int(agent.context_window), "set on the agent"
    cfg = get_settings()
    pid, model = ((agent.fallback_provider_id, agent.fallback_model) if on_fallback
                  else (agent.provider_id, agent.model))
    p = None
    if pid:
        with db.session() as s:
            p = s.get(ModelProvider, pid)
    if p is not None:
        found, source = await detect_window(p, model)
        if found:
            return found, source
        if p.kind == ProviderKind.ollama:
            return int(cfg["local_window"]), "a guess: Ollama's smallest default (set it on the agent)"
        return int(cfg["cloud_window"]), "the default for cloud models"
    return int(cfg["cloud_window"]), "the default"


# --------------------------------------------------------------------------- compaction


def transcript(messages: list[ModelMessage], clip: int = 600) -> str:
    """The messages as plain text for the summariser."""
    out = []
    for m in messages:
        for p in m.parts:
            if isinstance(p, UserPromptPart):
                text = p.content if isinstance(p.content, str) else _part_text(p)
                out.append(("Earlier summary:\n" + text[len(SUMMARY_PREFIX):]) if text.startswith(SUMMARY_PREFIX)
                           else f"User: {text}")
            elif isinstance(p, TextPart):
                if p.content.strip() and p.content != SUMMARY_ACK:
                    out.append(f"Assistant: {p.content}")
            elif isinstance(p, ToolCallPart):
                out.append(f"Assistant called {_part_text(p)[:clip]}")
            elif isinstance(p, ToolReturnPart):
                t = _part_text(p)
                out.append(f"Tool {p.tool_name} returned: {t[:clip]}{'…' if len(t) > clip else ''}")
    return "\n".join(out)


SUMMARY_RULES = """\
You compress the earlier part of a conversation between a user and an AI agent named
{name}, so the agent can carry on without the full text. Write a summary of at most
{max_words} words, in this order:
- What the user wants overall, and any standing instructions or preferences they gave.
- Decisions made and things that are done (with their results: names, hosts, numbers, IDs).
- Open questions and work still in progress.
- Facts discovered with tools that are likely to matter again.
Keep exact identifiers (hostnames, IPs, file paths, versions, ticket numbers). Leave out
small talk and anything superseded later. Write plain bullet points; no preamble."""


async def summarise(agent: AgentDef, text: str, window: int) -> tuple[str, str]:
    """A summary of `text`, in pieces that fit the summariser's window. Returns
    (summary, model description)."""
    from pydantic_ai import Agent

    from .llm import build_agent_model, build_model_for, describe_model

    cfg = get_settings()
    if cfg.get("model"):
        model = build_model_for(cfg.get("provider_id"), cfg["model"], "the summary model")
        name = describe_model(cfg.get("provider_id"), cfg["model"])
        piece_tokens = max(1500, int(cfg["cloud_window"] * 0.4))
    else:
        model, name = build_agent_model(agent), describe_model(agent.provider_id, agent.model)
        piece_tokens = max(800, int(window * 0.5) - int(cfg["summary_max_tokens"]))
    max_words = max(80, int(cfg["summary_max_tokens"] * 0.7))
    writer = Agent(model, instructions=SUMMARY_RULES.format(name=agent.name, max_words=max_words),
                   name=f"{agent.name} (summarising)")
    piece_chars = int(piece_tokens * 3.5)
    summary = ""
    for start in range(0, max(1, len(text)), piece_chars):
        chunk = text[start:start + piece_chars]
        prompt = (f"Summary so far:\n{summary}\n\nMore of the conversation:\n{chunk}" if summary
                  else f"The conversation:\n{chunk}")
        result = await writer.run(prompt)
        summary = str(result.output).strip()
    return summary, name


@dataclasses.dataclass
class Compaction:
    turns: int
    before: int
    after: int
    summary: str
    method: str  # summary | dropped
    model: str = ""


def _plan(messages: list[ModelMessage], budget: int, keep_ratio: float) -> int | None:
    """Where to cut so the kept recent turns fit their share; None if nothing can go."""
    starts = turn_starts(messages)
    if is_summary(messages):
        starts = [i for i in starts if i > 0]
    if len(starts) < 2:
        return None
    keep_budget = max(1, int(budget * keep_ratio))
    cut = starts[-1]  # always keep the latest turn
    for i in reversed(starts[:-1]):
        if history_tokens(messages[i:]) > keep_budget:
            break
        cut = i
    first_real = starts[0]
    return cut if cut > first_real or (is_summary(messages) and cut > 0) else None


async def compact(agent: AgentDef, messages: list[ModelMessage], *, budget: int,
                  window: int) -> tuple[list[ModelMessage], Compaction | None]:
    """Fit the history into `budget` tokens: summarise the older turns, keep the recent ones."""
    cfg = get_settings()
    before = history_tokens(messages)
    if not cfg["compaction"] or before <= budget:
        return messages, None
    cut = _plan(messages, budget, float(cfg["keep_ratio"]))
    if cut is None:
        return messages, None
    old, recent = messages[:cut], messages[cut:]
    turns = len([i for i in turn_starts(old) if not (i == 0 and is_summary(old))])
    try:
        summary, model = await summarise(agent, transcript(old), window)
        if not summary:
            raise ValueError("the summary came back empty")
        head: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content=SUMMARY_PREFIX + summary)]),
                                    ModelResponse(parts=[TextPart(content=SUMMARY_ACK)])]
        method = "summary"
    except Exception as e:
        log.warning("summarising %s's conversation failed (%s); dropping older turns", agent.name, e)
        summary, model, method = "", "", "dropped"
        head = [ModelRequest(parts=[UserPromptPart(content=DROPPED_NOTE)]),
                ModelResponse(parts=[TextPart(content="Understood.")])]
    out = head + recent
    return out, Compaction(turns=turns, before=before, after=history_tokens(out), summary=summary,
                           method=method, model=model)


def make_trimmer(budget_tokens: int):
    """A history processor for one run: the usual trim of old tool results, then harder
    trimming when the run is about to outgrow the window."""
    from .runtime import trim_old_tool_results

    def trim(messages: list[ModelMessage]) -> list[ModelMessage]:
        out = list(trim_old_tool_results(messages))
        if budget_tokens <= 0 or history_tokens(out) <= budget_tokens:
            return out
        returns = [(i, j) for i, m in enumerate(out) if isinstance(m, ModelRequest)
                   for j, p in enumerate(m.parts) if isinstance(p, ToolReturnPart)]
        for limit, keep in ((400, 2), (120, 1)):
            for i, j in returns[:-keep] if keep else returns:
                p = out[i].parts[j]
                text = _part_text(p)
                if len(text) > limit:
                    parts = list(out[i].parts)
                    parts[j] = dataclasses.replace(p, content=text[:limit] + f"… [shortened to fit the context window; "
                                                                             f"call {p.tool_name} again if you need it]")
                    out[i] = dataclasses.replace(out[i], parts=parts)
            if history_tokens(out) <= budget_tokens:
                break
        return out

    return trim
