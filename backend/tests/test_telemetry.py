"""Tracing: one trace per conversation turn across workers, Phoenix session grouping,
OpenInference span kinds, and no screenshots in spans."""

from __future__ import annotations

import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent

from app import telemetry

from .test_vision import _setup as vision_setup

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def tracing():
    telemetry.setup_telemetry(exporter=EXPORTER, force=True)
    yield
    Agent.instrument_all(False)


@pytest.fixture(autouse=True)
def clear():
    EXPORTER.clear()


def spans():
    return EXPORTER.get_finished_spans()


async def settle(hq):
    """Let job spans close (they end just after the job is marked done), and keep only
    spans from this test's jobs."""
    import asyncio
    await asyncio.sleep(0.3)
    ids = {j.id for j in hq.jobs()}
    return [s for s in spans() if s.attributes.get("agent_hq.job_id") in ids]


def job_spans(kind, pool=None):
    return [s for s in (pool if pool is not None else spans()) if s.attributes.get("agent_hq.job_kind") == kind
            and s.name.startswith(f"{kind} ")]


def ancestors(span, by_id):
    out = []
    while span.parent is not None and span.parent.span_id in by_id:
        span = by_id[span.parent.span_id]
        out.append(span)
    return out


async def _team(hq, boss="fake:boss"):
    w = await hq.agent(name="Scout", description="research", model="fake:worker", pool="gpu")
    b = await hq.agent(name="Chief", model=boss, delegates=[w["id"]])
    return await hq.thread(b["id"])


async def test_turn_and_delegation_on_two_workers_are_one_trace(hq):
    await hq.start_worker(name="m1", pools=["default"])
    await hq.start_worker(name="m2", pools=["gpu"])
    t = await _team(hq)
    await hq.say(t["id"], "check feeds")
    await hq.wait_agent_replies(t["id"], 1)
    mine = await settle(hq)

    [turn], [task] = job_spans("turn", mine), job_spans("task", mine)
    assert turn.attributes["agent_hq.worker"] == "m1" and task.attributes["agent_hq.worker"] == "m2"
    assert task.context.trace_id == turn.context.trace_id
    by_id = {s.context.span_id: s for s in spans()}
    chain = ancestors(task, by_id)
    assert turn in chain  # the delegated job hangs under the turn that delegated it
    assert any(s.attributes.get("openinference.span.kind") == "TOOL" for s in chain)  # via the delegate call
    assert turn.attributes["output.value"] == "Report: Worker finished"


async def test_every_span_in_a_job_belongs_to_the_session(hq):
    await hq.start_worker(name="m1", pools=["default", "gpu"])
    t = await _team(hq)
    await hq.say(t["id"], "go")
    await hq.wait_agent_replies(t["id"], 1)
    inside = await settle(hq)
    assert inside and all(s.attributes["session.id"] == t["id"] for s in inside)
    kinds = {s.attributes.get("openinference.span.kind") for s in inside}
    assert {"LLM", "TOOL", "AGENT", "CHAIN"} <= kinds  # Phoenix's native span kinds
    meta = json.loads(job_spans("task", inside)[0].attributes["metadata"])
    assert meta["agent"] == "Scout" and meta["pool"] == "gpu"


async def test_background_report_joins_the_same_trace(hq):
    await hq.start_worker(name="m1", pools=["default", "gpu"])
    t = await _team(hq, boss="fake:boss-bg")
    await hq.say(t["id"], "long job")
    await hq.wait_agent_replies(t["id"], 2)
    mine = await settle(hq)
    turns, [task] = job_spans("turn", mine), job_spans("task", mine)
    assert len(turns) == 2
    assert {s.context.trace_id for s in [*turns, task]} == {turns[0].context.trace_id}


async def test_screenshots_never_enter_spans(hq, tmp_path, monkeypatch):
    await hq.start_worker()
    _, t, _ = await vision_setup(hq, tmp_path, monkeypatch)
    await hq.say(t["id"], "sign in please")
    await hq.wait_agent_replies(t["id"], 1)
    await settle(hq)
    vision_llm = [s for s in spans() if s.attributes.get("openinference.span.kind") == "LLM"
                  and "vision" in json.dumps(dict(s.attributes), default=str)]
    assert vision_llm  # the vision model's calls are traced...
    for s in spans():
        blob = json.dumps(dict(s.attributes), default=str)
        assert "iVBORw0KGgo" not in blob  # ...but no base64 PNG data anywhere
