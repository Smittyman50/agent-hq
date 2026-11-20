"""The Automatic request time limit: each model's speed, measured from its own requests,
and the deadline that follows.

Regression (2026-11-19): a fixed 420-second limit cut Qwen off while it wrote the game's
first file at 24 tokens a second; Ollama sends a tool call only when it's finished, so
nothing short of the model's own speed tells working from stuck."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent, ModelResponse, TextPart
from pydantic_ai.exceptions import ModelRequestTimeout
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from app import speed
from app.models import AgentDef
from app.runtime import limit_settings
from app.speed import Speed, TimedModel, deadline, update


def test_a_long_answer_measures_writing_and_a_short_one_reading():
    s = Speed()
    # 6,000 tokens in 250 s with nothing to read: 24 a second.
    assert update(s, seconds=250, input_tokens=30_000, cached_tokens=30_000, output_tokens=6000)
    assert s.write_tps == pytest.approx(24)
    # A 100-token tool call that had 20,000 uncached tokens to read in about 24 s.
    assert update(s, seconds=24 + 100 / 24, input_tokens=20_000, cached_tokens=0, output_tokens=100)
    assert s.read_tps == pytest.approx(20_000 / 24, rel=0.01)
    # In between, or too little to read: nothing learned.
    assert not update(s, seconds=10, input_tokens=30_000, cached_tokens=29_900, output_tokens=200)
    assert s.samples == 2


def test_the_average_moves_toward_new_speeds():
    s = Speed(write_tps=24.0, read_tps=800.0)
    update(s, seconds=100, input_tokens=1000, cached_tokens=1000, output_tokens=4000)  # 40 a second
    assert 24 < s.write_tps < 40


def test_deadline_follows_the_speed_and_the_output_limit():
    assert deadline(Speed(), prompt_tokens=30_000, max_output_tokens=16_384) == 1800  # unknown yet
    assert deadline(Speed(), prompt_tokens=30_000, max_output_tokens=16_384, starting=420) == 420
    s = Speed(write_tps=24.0, read_tps=800.0)
    # 30,000 / 800 + 16,384 / 24 = 720 s, times 1.25, plus 30.
    assert deadline(s, prompt_tokens=30_000, max_output_tokens=16_384) == pytest.approx(930, abs=1)
    # A faster model gets less; no output limit assumes 16,384; and it stays within bounds.
    assert deadline(Speed(write_tps=80, read_tps=3000), prompt_tokens=30_000, max_output_tokens=16_384) < 400
    assert deadline(s, prompt_tokens=0, max_output_tokens=None) == deadline(s, prompt_tokens=0, max_output_tokens=16_384)
    assert deadline(Speed(write_tps=1000, read_tps=1e6), prompt_tokens=10, max_output_tokens=256) == 60
    assert deadline(Speed(write_tps=0.5), prompt_tokens=10, max_output_tokens=262_144) == 7200


def test_automatic_leaves_the_fixed_deadline_out():
    fixed = AgentDef(name="a", model="m", request_timeout_s=420)
    assert limit_settings(fixed)["request_timeout"] == 420
    auto = AgentDef(name="a", model="m", request_timeout_s=420, request_timeout_auto=True)
    assert "request_timeout" not in limit_settings(auto)


class FakeRedis:
    def __init__(self):
        self.d = {}

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v):
        self.d[k] = v


def slow_model(delay: float, out: int):
    async def answer(messages, info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(delay)
        return ModelResponse(parts=[TextPart("done")],
                             usage=RequestUsage(input_tokens=1000, cache_read_tokens=1000, output_tokens=out))
    return FunctionModel(answer, model_name="slow")


async def test_requests_are_measured_and_the_speed_is_kept():
    r = FakeRedis()
    m = TimedModel(slow_model(0.2, 400), redis=r, provider_id="p1", model="qwen", max_output_tokens=8192)
    assert (await Agent(m).run("go")).output == "done"
    kept = await speed.load(r, "p1", "qwen")
    assert kept.write_tps == pytest.approx(2000, rel=0.3)  # 400 tokens in about 0.2 s
    # The next run of any agent on that model starts from it.
    m2 = TimedModel(slow_model(0, 1), redis=r, provider_id="p1", model="qwen", max_output_tokens=8192)
    await Agent(m2).run("go")
    assert m2.speed.write_tps == pytest.approx(kept.write_tps)


async def test_a_request_past_its_deadline_raises_the_usual_timeout(monkeypatch):
    monkeypatch.setattr(speed, "MIN_DEADLINE_S", 0.1)
    monkeypatch.setattr(speed, "OVERHEAD_S", 0.0)
    r = FakeRedis()
    # Measured: 1,000 tokens a second, so 100 output tokens may take about 0.1 s.
    await speed.save(r, "p1", "qwen", Speed(write_tps=1000.0, read_tps=1e9))
    m = TimedModel(slow_model(2.0, 100), redis=r, provider_id="p1", model="qwen", max_output_tokens=100)
    with pytest.raises(ModelRequestTimeout):
        await Agent(m).run("go")
    # Measuring only (not Automatic): the same slow request finishes.
    m = TimedModel(slow_model(0.5, 100), redis=r, provider_id="p1", model="qwen", max_output_tokens=100,
                   enforce=False)
    assert (await Agent(m).run("go")).output == "done"
