"""Everything that goes through Valkey/Redis.

Key layout (all prefixed "ahq:"):
  ahq:jobs:<pool>        stream of job IDs for a worker pool (consumer group "workers")
  ahq:result:<job_id>    list holding a finished task's WorkResult, for a waiting delegator
  ahq:events:<thread_id> pub/sub channel of live activity for the UI
  ahq:lock:thread:<id>   lock so turns of one conversation never overlap, across machines
  ahq:worker:<name>      heartbeat key of a live worker (expires if it dies)
  ahq:cooldown:<sub_id>  MQTT subscription cooldown marker
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

GROUP = "workers"


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


class Broker:
    def __init__(self, url: str) -> None:
        self.url = url
        self.r = aioredis.from_url(url, decode_responses=True)

    async def close(self) -> None:
        await self.r.aclose()

    # ------------------------------------------------------------------ job streams

    @staticmethod
    def stream(pool: str) -> str:
        return f"ahq:jobs:{pool}"

    async def ensure_group(self, pool: str) -> None:
        try:
            await self.r.xgroup_create(self.stream(pool), GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def enqueue(self, pool: str, job_id: str) -> str:
        await self.ensure_group(pool)
        return await self.r.xadd(self.stream(pool), {"job_id": job_id})

    async def read(self, pools: list[str], consumer: str, block_ms: int) -> list[tuple[str, str, str]]:
        """Read at most one new entry. Returns [(pool, entry_id, job_id)]."""
        streams = {self.stream(p): ">" for p in pools}
        resp = await self.r.xreadgroup(GROUP, consumer, streams, count=1, block=block_ms)
        out = []
        for stream, entries in resp or []:
            pool = stream.split(":", 2)[2]
            for entry_id, fields in entries:
                out.append((pool, entry_id, fields.get("job_id", "")))
        return out

    async def claim_stale(self, pool: str, consumer: str, min_idle_ms: int,
                          count: int = 1) -> list[tuple[str, str, str]]:
        """Take over entries a dead worker never acknowledged."""
        _, entries, *_ = await self.r.xautoclaim(self.stream(pool), GROUP, consumer,
                                                 min_idle_time=min_idle_ms, start_id="0-0",
                                                 count=count)
        return [(pool, eid, fields.get("job_id", "")) for eid, fields in entries if fields]

    async def touch(self, pool: str, consumer: str, entry_id: str) -> None:
        """Heartbeat: reset an entry's idle time so nobody else claims it."""
        await self.r.xclaim(self.stream(pool), GROUP, consumer, 0, [entry_id], justid=True)

    async def ack(self, pool: str, entry_id: str) -> None:
        await self.r.xack(self.stream(pool), GROUP, entry_id)
        await self.r.xdel(self.stream(pool), entry_id)

    async def pending_count(self, pool: str) -> int:
        try:
            info = await self.r.xpending(self.stream(pool), GROUP)
            return int(info["pending"])
        except ResponseError:
            return 0

    # ------------------------------------------------------------------ results

    async def push_result(self, job_id: str, result: dict) -> None:
        key = f"ahq:result:{job_id}"
        await self.r.rpush(key, json.dumps(result))
        await self.r.expire(key, 86400)

    async def wait_result(self, job_id: str, timeout: float) -> dict | None:
        got = await self.r.blpop([f"ahq:result:{job_id}"], timeout=timeout)
        return json.loads(got[1]) if got else None

    # ------------------------------------------------------------------ live events

    async def publish(self, thread_id: str, type_: str, **data: Any) -> None:
        event = {"type": type_, "thread_id": thread_id, "ts": _ts(), **data}
        await self.r.publish(f"ahq:events:{thread_id}", json.dumps(event, default=str))

    async def events(self, thread_id: str) -> AsyncIterator[dict]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(f"ahq:events:{thread_id}")
        try:
            while True:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=25)
                yield json.loads(msg["data"]) if msg else {"type": "ping"}
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()

    # ------------------------------------------------------------------ locks & cooldowns

    def thread_lock(self, thread_id: str, ttl: float):
        return self.r.lock(f"ahq:lock:thread:{thread_id}", timeout=ttl, blocking_timeout=None)

    async def try_cooldown(self, key: str, seconds: int) -> bool:
        """True if not in cooldown (and starts one)."""
        if seconds <= 0:
            return True
        return bool(await self.r.set(f"ahq:cooldown:{key}", "1", nx=True, ex=seconds))

    # ------------------------------------------------------------------ worker registry

    async def heartbeat_worker(self, name: str, info: dict, ttl: int) -> None:
        await self.r.set(f"ahq:worker:{name}", json.dumps({**info, "seen": _ts()}), ex=ttl)

    async def remove_worker(self, name: str) -> None:
        await self.r.delete(f"ahq:worker:{name}")

    async def workers(self) -> list[dict]:
        out = []
        async for key in self.r.scan_iter("ahq:worker:*"):
            raw = await self.r.get(key)
            if raw:
                out.append(json.loads(raw))
        return sorted(out, key=lambda w: w.get("name", ""))
