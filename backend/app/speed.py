"""How fast each model reads prompts and writes answers, measured from its own requests,
and the request deadline that follows (the Automatic request time limit).

Why: Ollama sends a tool call in one piece once the model has finished it, so a model
writing a 10,000-token file looks exactly like a stuck one until it is done. A fixed
deadline either cuts off good work (seen 2026-11-19: 420 s, while Qwen wrote the game's
first file at 24 tokens a second) or waits too long on a stuck model. What does bound a
healthy request is the model's own speed: it can't take longer than reading the prompt
plus writing max_output_tokens. Past that it is stuck, so that is the deadline.

The speeds are kept per model server and model in Valkey, as a moving average, so they
carry across runs and follow the model when an agent switches to another one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from opentelemetry import trace
from pydantic_ai.exceptions import ModelRequestTimeout
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from . import context

log = logging.getLogger(__name__)

ALPHA = 0.3                  # weight of the newest sample in the moving average
MIN_WRITE_SAMPLE = 300       # output tokens: shorter answers say more about latency than speed
MIN_READ_SAMPLE = 500        # prompt tokens the model had to read (not served from its cache)
SHORT_ANSWER = 150           # output tokens: an answer this short is mostly prompt reading
ASSUMED_READ_TPS = 300.0     # until reading has been measured (on the slow side, so generous)
ASSUMED_MAX_OUTPUT = 16_384  # when the agent sets no output limit
SAFETY = 1.25                # headroom on top of the computed time
OVERHEAD_S = 30.0            # connection, model loading, scheduling
MIN_DEADLINE_S = 60.0
MAX_DEADLINE_S = 7200.0
STARTING_DEADLINE_S = 1800.0  # before the model's speed is known, unless the agent sets one


@dataclass
class Speed:
    write_tps: float | None = None  # output tokens a second
    read_tps: float | None = None   # uncached prompt tokens a second
    samples: int = 0
    updated: float = 0.0


def key(provider_id: str | None, model: str) -> str:
    return f"ahq:speed:{provider_id or '-'}:{model}"


async def load(r: Any, provider_id: str | None, model: str) -> Speed:
    try:
        raw = await r.get(key(provider_id, model))
        return Speed(**json.loads(raw)) if raw else Speed()
    except Exception as e:  # measuring must never stop a run
        log.warning("couldn't load model speed for %s: %s", model, e)
        return Speed()


async def save(r: Any, provider_id: str | None, model: str, s: Speed) -> None:
    try:
        await r.set(key(provider_id, model), json.dumps(asdict(s)))
    except Exception as e:
        log.warning("couldn't save model speed for %s: %s", model, e)


def _avg(old: float | None, new: float) -> float:
    return new if old is None else (1 - ALPHA) * old + ALPHA * new


def update(s: Speed, *, seconds: float, input_tokens: int, cached_tokens: int,
           output_tokens: int) -> bool:
    """Fold one finished request into the averages; True when it taught us something.

    One request takes reading time plus writing time and reports only the total, so a
    short answer measures reading and a long one, less the reading time, measures writing.
    Until reading is measured it counts as free, which makes writing look slower and the
    deadline longer: the safe side."""
    if seconds <= 0:
        return False
    read = max(0, input_tokens - cached_tokens)
    learned = False
    if output_tokens <= SHORT_ANSWER and read >= MIN_READ_SAMPLE:
        writing = output_tokens / s.write_tps if s.write_tps else 0.0
        if seconds - writing > 0.05:
            s.read_tps = _avg(s.read_tps, read / (seconds - writing))
            learned = True
    elif output_tokens >= MIN_WRITE_SAMPLE:
        reading = read / s.read_tps if s.read_tps else 0.0
        writing = max(seconds - reading, seconds * 0.2)
        s.write_tps = _avg(s.write_tps, output_tokens / writing)
        learned = True
    if learned:
        s.samples += 1
        s.updated = time.time()
    return learned


def deadline(s: Speed, *, prompt_tokens: int, max_output_tokens: int | None,
             starting: float | None = None) -> float:
    """Seconds a healthy request can take: reading the whole prompt (as if nothing were
    cached) plus writing a full answer, with headroom. Before writing has been measured:
    `starting` (the agent's own setting) or STARTING_DEADLINE_S."""
    if not s.write_tps:
        return float(starting or STARTING_DEADLINE_S)
    reading = prompt_tokens / (s.read_tps or ASSUMED_READ_TPS)
    writing = (max_output_tokens or ASSUMED_MAX_OUTPUT) / s.write_tps
    return min(MAX_DEADLINE_S, max(MIN_DEADLINE_S, (reading + writing) * SAFETY + OVERHEAD_S))


class TimedModel(WrapperModel):
    """The agent's main model, timed: each request gets the deadline its measured speed
    allows, and each finished request updates that speed. The timings also go on the
    request's trace span (agent_hq.request.*), so Phoenix shows them."""

    def __init__(self, wrapped: Any, *, redis: Any, provider_id: str | None, model: str,
                 max_output_tokens: int | None, starting: float | None = None,
                 enforce: bool = True) -> None:
        super().__init__(wrapped)
        self.enforce = enforce  # False: measure only, no deadline of its own
        self.r, self.provider_id, self.model = redis, provider_id, model
        self.max_output_tokens, self.starting = max_output_tokens, starting
        self.speed: Speed | None = None
        self._last_input = 0      # tokens the previous request's prompt really took
        self._last_count = 0      # messages it carried

    def _prompt_estimate(self, messages: list[ModelMessage]) -> int:
        if self._last_input and len(messages) >= self._last_count:
            return self._last_input + context.history_tokens(messages[self._last_count:])
        # First request: the history plus room for instructions and tool definitions.
        return int(context.history_tokens(messages) * 1.3) + 8000

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        if self.speed is None:
            self.speed = await load(self.r, self.provider_id, self.model)
        prompt = self._prompt_estimate(messages)
        limit = deadline(self.speed, prompt_tokens=prompt, max_output_tokens=self.max_output_tokens,
                         starting=self.starting)
        span = trace.get_current_span()
        if self.enforce:
            span.set_attribute("agent_hq.request.deadline_s", round(limit, 1))
            span.set_attribute("agent_hq.request.deadline_basis",
                               "measured" if self.speed.write_tps else "starting")
        started = time.monotonic()
        try:
            async with asyncio.timeout(limit if self.enforce else None) as cm:
                response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except TimeoutError:
            if cm.expired():
                raise ModelRequestTimeout(self.model_name, round(limit)) from None
            raise
        seconds = time.monotonic() - started
        u = response.usage
        inp, cached, out = (int(u.input_tokens or 0), int(u.cache_read_tokens or 0),
                            int(u.output_tokens or 0))
        self._last_input, self._last_count = inp, len(messages) + 1
        span.set_attribute("agent_hq.request.seconds", round(seconds, 2))
        if out and seconds > 0:
            span.set_attribute("agent_hq.request.output_tps", round(out / seconds, 2))
        if update(self.speed, seconds=seconds, input_tokens=inp, cached_tokens=cached, output_tokens=out):
            await save(self.r, self.provider_id, self.model, self.speed)
        if self.speed.write_tps:
            span.set_attribute("agent_hq.model.write_tps", round(self.speed.write_tps, 2))
        if self.speed.read_tps:
            span.set_attribute("agent_hq.model.read_tps", round(self.speed.read_tps, 1))
        return response
