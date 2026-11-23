"""Live activity: what each agent is doing right now, and how hard it is working.

The usage ledger (UsageRecord) gets one row when a run *ends*, so a goal turn that runs
for two hours shows nothing until it's over. This counts as things happen instead: every
model request and every tool call, from every worker, goes into Valkey the moment it
finishes, and each running job keeps a "now" record saying whether its agent is waiting
on the model or running a tool, and since when. The Activity page reads it every few
seconds (GET /api/activity).

Keys (all prefixed "ahq:act:"):
  min:<epoch minute>   hash "<agent_id>|<metric>" -> count, one per minute, kept MINUTE_TTL
  hour:<epoch hour>    the same per hour, kept HOUR_TTL (for windows longer than 3 hours)
  tmin:<epoch minute>, thour:<epoch hour>
                       hash "<agent_id>|<tool name>" -> calls, kept like min: and hour:
  job:<job_id>         hash: the running job and what its agent is doing now. Refreshed on
                       every step, deleted when the job ends, expires after JOB_TTL anyway
                       (a worker that died can't leave it behind)
  feed                 stream of recent steps across all agents (FEED_MAX kept)

Metrics: requests, input (prompt tokens), cached (of those, read from the model's
cache), output, llm_ms (time in model requests), req_errors, tools, tool_errors, tool_ms,
runs (jobs finished).

Counting must never slow down or break a run: every write is one pipelined round trip,
and failures are logged and dropped.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

log = logging.getLogger(__name__)

P = "ahq:act:"
MINUTE_TTL = 26 * 3600
HOUR_TTL = 8 * 86400
JOB_TTL = 3 * 3600      # a request can legitimately take up to speed.MAX_DEADLINE_S (2 h)
FEED_MAX = 300
METRICS = ("requests", "input", "cached", "output", "llm_ms", "req_errors",
           "tools", "tool_errors", "tool_ms", "runs")


def _now() -> float:
    return time.time()


def _iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else _now(), timezone.utc).isoformat()


def _minute(t: float) -> int:
    return int(t // 60)


def _hour(t: float) -> int:
    return int(t // 3600)


def current_job() -> dict[str, Any]:
    """The job this code runs in (set by telemetry.job_span), else {}."""
    from .telemetry import current_job_attributes

    a = current_job_attributes() or {}
    return {"job_id": a.get("agent_hq.job_id"), "agent_id": a.get("agent_hq.agent_id"),
            "agent": a.get("agent_hq.agent")}


async def _count(r: Any, agent_id: str, values: dict[str, float], *, tool: str | None = None,
                 job_id: str | None = None, state: dict[str, Any] | None = None,
                 feed: dict[str, Any] | None = None) -> None:
    """Add to the minute and hour buckets, and optionally update a job's state and the feed."""
    if not agent_id:
        return
    t = _now()
    mk, hk = f"{P}min:{_minute(t)}", f"{P}hour:{_hour(t)}"
    try:
        async with r.pipeline(transaction=False) as p:
            for k, v in values.items():
                if not v:
                    continue
                field = f"{agent_id}|{k}"
                if isinstance(v, float):
                    p.hincrbyfloat(mk, field, v)
                    p.hincrbyfloat(hk, field, v)
                else:
                    p.hincrby(mk, field, int(v))
                    p.hincrby(hk, field, int(v))
            p.expire(mk, MINUTE_TTL)
            p.expire(hk, HOUR_TTL)
            if tool:
                for tk, ttl in ((f"{P}tmin:{_minute(t)}", MINUTE_TTL), (f"{P}thour:{_hour(t)}", HOUR_TTL)):
                    p.hincrby(tk, f"{agent_id}|{tool}", 1)
                    p.expire(tk, ttl)
            if job_id and state:
                jk = f"{P}job:{job_id}"
                p.hset(jk, mapping={k: ("" if v is None else str(v)) for k, v in state.items()})
                p.expire(jk, JOB_TTL)
            if feed:
                p.xadd(f"{P}feed", {"e": json.dumps({"ts": _iso(t), **feed}, default=str)},
                       maxlen=FEED_MAX, approximate=True)
            await p.execute()
    except Exception as e:
        log.warning("couldn't record activity: %s", e)


async def _set_state(r: Any, job_id: str | None, **state: Any) -> None:
    if not job_id:
        return
    try:
        jk = f"{P}job:{job_id}"
        # Only for a job that started (job_started wrote "agent_id"); a stray update after
        # the job ended must not bring its record back.
        if not await r.hexists(jk, "agent_id"):
            return
        async with r.pipeline(transaction=False) as p:
            p.hset(jk, mapping={k: ("" if v is None else str(v)) for k, v in state.items()})
            p.expire(jk, JOB_TTL)
            await p.execute()
    except Exception as e:
        log.warning("couldn't record activity state: %s", e)


# --------------------------------------------------------------------------- jobs


@contextlib.asynccontextmanager
async def job_running(r: Any, job: Any, agent: Any, worker: str) -> AsyncIterator[None]:
    """Marks a job as running for as long as the block lasts."""
    jk = f"{P}job:{job.id}"
    started = _now()
    task = (job.task or "").strip()
    try:
        async with r.pipeline(transaction=False) as p:
            p.delete(jk)
            p.hset(jk, mapping={
                "job_id": job.id, "agent_id": agent.id, "agent": agent.name,
                "parent_agent_id": getattr(agent, "parent_agent_id", None) or "",
                "kind": str(getattr(job.kind, "value", job.kind)),
                "thread_id": job.origin_thread_id or "", "worker": worker, "pool": job.pool,
                "depth": job.depth, "task": task[:300], "started": _iso(started),
                "state": "starting", "since": _iso(started), "detail": "",
                "requests": 0, "tools": 0, "input": 0, "output": 0})
            p.expire(jk, JOB_TTL)
            p.xadd(f"{P}feed", {"e": json.dumps({
                "ts": _iso(started), "agent_id": agent.id, "agent": agent.name, "job_id": job.id,
                "type": "job_started", "kind": str(getattr(job.kind, "value", job.kind)),
                "detail": task.splitlines()[0][:160] if task else ""})},
                maxlen=FEED_MAX, approximate=True)
            await p.execute()
    except Exception as e:
        log.warning("couldn't record job start: %s", e)
    ok = False
    try:
        yield
        ok = True
    finally:
        seconds = round(_now() - started, 1)
        try:
            await r.delete(jk)
        except Exception as e:
            log.warning("couldn't clear job activity: %s", e)
        await _count(r, agent.id, {"runs": 1}, feed={
            "agent_id": agent.id, "agent": agent.name, "job_id": job.id,
            "type": "job_finished" if ok else "job_ended", "seconds": seconds})


# --------------------------------------------------------------------------- tools


async def tool_started(r: Any, *, agent_id: str, job_id: str | None, tool: str) -> float:
    await _set_state(r, job_id, state="tool", since=_iso(), detail=tool)
    return time.monotonic()


async def tool_finished(r: Any, *, agent_id: str, agent: str, job_id: str | None, tool: str,
                        started: float, error: str | None = None) -> None:
    ms = int((time.monotonic() - started) * 1000)
    state = None
    if job_id:
        state = {"state": "between", "since": _iso(), "detail": "", "last_tool": tool}
    await _count(r, agent_id, {"tools": 1, "tool_ms": ms, "tool_errors": 1 if error else 0},
                 tool=tool, job_id=job_id if job_id and await _alive(r, job_id) else None,
                 state=state, feed={"agent_id": agent_id, "agent": agent, "job_id": job_id,
                                    "type": "tool", "tool": tool, "ms": ms,
                                    "error": (error or "")[:200] or None})
    if job_id:
        await _bump(r, job_id, "tools", 1)


async def _alive(r: Any, job_id: str) -> bool:
    try:
        return bool(await r.hexists(f"{P}job:{job_id}", "agent_id"))
    except Exception:
        return False


async def _bump(r: Any, job_id: str, field: str, n: int) -> None:
    try:
        if n and await _alive(r, job_id):
            await r.hincrby(f"{P}job:{job_id}", field, n)
    except Exception as e:
        log.warning("couldn't update job activity: %s", e)


# --------------------------------------------------------------------------- model requests


class MeteredModel(WrapperModel):
    """Counts every model request the moment it finishes: tokens, time, errors. Wraps the
    agent's model (main or fallback), outside speed.TimedModel."""

    def __init__(self, wrapped: Any, *, redis: Any, agent_id: str, agent_name: str,
                 model: str, fallback: bool = False) -> None:
        super().__init__(wrapped)
        self.r, self.agent_id, self.agent_name = redis, agent_id, agent_name
        self.shown_as = model + (" (fallback)" if fallback else "")

    async def request(self, messages: list[ModelMessage], model_settings: ModelSettings | None,
                      model_request_parameters: ModelRequestParameters) -> ModelResponse:
        job_id = current_job().get("job_id")
        await _set_state(self.r, job_id, state="model", since=_iso(), detail=self.shown_as)
        started = time.monotonic()
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except BaseException as e:
            ms = int((time.monotonic() - started) * 1000)
            await _count(self.r, self.agent_id, {"requests": 1, "req_errors": 1, "llm_ms": ms},
                         feed={"agent_id": self.agent_id, "agent": self.agent_name,
                               "job_id": job_id, "type": "model", "model": self.shown_as, "ms": ms,
                               "error": (str(e) or type(e).__name__)[:200]})
            raise
        ms = int((time.monotonic() - started) * 1000)
        u = response.usage
        inp, cached, out = (int(u.input_tokens or 0), int(u.cache_read_tokens or 0),
                            int(u.output_tokens or 0))
        state = None
        if job_id and await _alive(self.r, job_id):
            state = {"state": "between", "since": _iso(), "detail": "", "model": self.shown_as}
        await _count(self.r, self.agent_id,
                     {"requests": 1, "input": inp, "cached": cached, "output": out, "llm_ms": ms},
                     job_id=job_id if state else None, state=state,
                     feed={"agent_id": self.agent_id, "agent": self.agent_name, "job_id": job_id,
                           "type": "model", "model": self.shown_as, "ms": ms, "input": inp,
                           "cached": cached, "output": out,
                           "finish": getattr(response, "finish_reason", None)})
        if state:
            try:
                async with self.r.pipeline(transaction=False) as p:
                    jk = f"{P}job:{job_id}"
                    p.hincrby(jk, "requests", 1)
                    p.hincrby(jk, "input", inp)
                    p.hincrby(jk, "output", out)
                    await p.execute()
            except Exception as e:
                log.warning("couldn't update job activity: %s", e)
        return response


# --------------------------------------------------------------------------- reading


WINDOWS = {15: 60, 60: 60, 180: 300, 1440: 3600, 10080: 6 * 3600}  # minutes -> bucket seconds


def _blank() -> dict[str, float]:
    return {m: 0 for m in METRICS}


async def snapshot(r: Any, window: int = 60) -> dict[str, Any]:
    """Everything the Activity page shows, for the last `window` minutes:
    totals and a time series per agent, top tools, running jobs and the recent feed."""
    window = window if window in WINDOWS else 60
    bucket_s = WINDOWS[window]
    t = _now()
    n = max(1, window * 60 // bucket_s)
    end = int(t // bucket_s) * bucket_s       # start of the current bucket
    starts = [end - (n - 1 - i) * bucket_s for i in range(n)]
    if bucket_s < 3600:
        ids, unit, kind, tkind = range(_minute(starts[0]), _minute(t) + 1), 60, "min", "tmin"
    else:
        ids, unit, kind, tkind = range(_hour(starts[0]), _hour(t) + 1), 3600, "hour", "thour"
    keys = [f"{P}{kind}:{i}" for i in ids]
    async with r.pipeline(transaction=False) as p:
        for k in keys:
            p.hgetall(k)
        for i in ids:
            p.hgetall(f"{P}{tkind}:{i}")
        raw = await p.execute()
    buckets, tool_rows = raw[:len(keys)], raw[len(keys):]

    agents: dict[str, dict[str, Any]] = {}

    def agent(aid: str) -> dict[str, Any]:
        if aid not in agents:
            agents[aid] = {"agent_id": aid, "totals": _blank(),
                           "series": {m: [0] * n for m in ("requests", "input", "output", "tools")},
                           "tools": {}}
        return agents[aid]

    for k, data in zip(keys, buckets):
        ts = int(k.rsplit(":", 1)[1]) * unit
        i = (ts - starts[0]) // bucket_s
        if i < 0 or i >= n:
            continue
        for field, v in (data or {}).items():
            aid, _, metric = field.rpartition("|")
            if metric not in METRICS:
                continue
            a = agent(aid)
            val = float(v)
            a["totals"][metric] += val
            if metric in a["series"]:
                a["series"][metric][i] += val
    for data in tool_rows:
        for field, v in (data or {}).items():
            aid, _, tool = field.partition("|")
            tools = agent(aid)["tools"]
            tools[tool] = tools.get(tool, 0) + int(v)

    jobs = []
    async for k in r.scan_iter(f"{P}job:*", count=200):
        j = await r.hgetall(k)
        if j.get("agent_id"):
            for f in ("requests", "tools", "input", "output", "depth"):
                j[f] = int(j.get(f) or 0)
            jobs.append(j)
    jobs.sort(key=lambda j: j.get("started", ""))

    feed = []
    for _eid, fields in await r.xrevrange(f"{P}feed", count=80):
        try:
            feed.append(json.loads(fields["e"]))
        except Exception:
            continue

    total = _blank()
    out = []
    for a in agents.values():
        for m, v in a["totals"].items():
            total[m] += v
        a["totals"] = {m: (round(v, 1) if isinstance(v, float) and not v.is_integer() else int(v))
                       for m, v in a["totals"].items()}
        a["series"] = {m: [int(x) for x in s] for m, s in a["series"].items()}
        a["top_tools"] = [{"tool": k, "calls": v}
                          for k, v in sorted(a.pop("tools").items(), key=lambda kv: -kv[1])[:8]]
        out.append(a)
    return {"window": window, "bucket_s": bucket_s, "now": _iso(t),
            "buckets": [_iso(s) for s in starts],
            "totals": {m: int(v) for m, v in total.items()},
            "agents": out, "jobs": jobs, "feed": feed}
