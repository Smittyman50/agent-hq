"""Everything that goes through Valkey/Redis.

Key layout (all prefixed "ahq:"):
  ahq:jobs:<pool>        stream of job IDs for a worker pool (consumer group "workers")
  ahq:result:<job_id>    list holding a finished task's WorkResult, for a waiting delegator
  ahq:events:<thread_id> pub/sub channel of live activity for the UI
  ahq:lock:thread:<id>   lock so turns of one conversation never overlap, across machines
  ahq:stop:<job_id>      set when you press Stop; the job's worker cancels it (see Runtime)
  ahq:worker:<name>      heartbeat key of a live worker (expires if it dies)
  ahq:cooldown:<sub_id>  MQTT subscription cooldown marker
  ahq:desktop:<server_id>:control   set while you have control of a desktop
  ahq:desktop:<server_id>:request   an agent's pending "please take over" request
  ahq:desktop:<server_id>:blocked   an agent tried the desktop while you had control
  ahq:desktop:<server_id>:handback  list a waiting agent blocks on until you hand back
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


def connect(url: str, **kwargs: Any) -> aioredis.Redis:
    """A Valkey/Redis client suited to long blocking reads.

    redis-py 8 gives every socket read a 5-second timeout by default, which breaks the
    blocking commands Agent HQ relies on: waiting for a delegated result (BLPOP for up to
    AHQ_DELEGATE_WAIT_TIMEOUT), waiting for you to hand a desktop back, and pub/sub. So
    reads may block as long as the command asks; dead connections are caught instead by
    TCP keepalive and a periodic health check, and connecting still times out quickly.
    """
    return aioredis.from_url(url, socket_timeout=None, socket_connect_timeout=10,
                             socket_keepalive=True, health_check_interval=30, **kwargs)


class Broker:
    def __init__(self, url: str) -> None:
        self.url = url
        self.r = connect(url, decode_responses=True)

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

    # ------------------------------------------------------------------ stopping jobs

    async def request_stop(self, job_id: str) -> None:
        """Ask whichever worker has (or will get) this job to stop it."""
        await self.r.set(f"ahq:stop:{job_id}", "1", ex=86400)

    async def stop_requested(self, job_id: str) -> bool:
        return bool(await self.r.exists(f"ahq:stop:{job_id}"))

    # ------------------------------------------------------------------ locks & cooldowns

    def thread_lock(self, thread_id: str, ttl: float):
        return self.r.lock(f"ahq:lock:thread:{thread_id}", timeout=ttl, blocking_timeout=None)

    async def first_seen(self, key: str, seconds: int) -> bool:
        """True the first time `key` is seen within `seconds` (webhook duplicate check)."""
        return bool(await self.r.set(f"ahq:hook:dup:{key}", "1", nx=True, ex=max(seconds, 1)))

    async def try_cooldown(self, key: str, seconds: int) -> bool:
        """True if not in cooldown (and starts one)."""
        if seconds <= 0:
            return True
        return bool(await self.r.set(f"ahq:cooldown:{key}", "1", nx=True, ex=seconds))

    # ------------------------------------------------------------------ desktop handoff

    @staticmethod
    def _dk(server_id: str, part: str) -> str:
        return f"ahq:desktop:{server_id}:{part}"

    async def desktop_state(self, server_id: str) -> dict:
        control, request, blocked = await self.r.mget(
            self._dk(server_id, "control"), self._dk(server_id, "request"),
            self._dk(server_id, "blocked"))
        return {"control": json.loads(control) if control else None,
                "request": json.loads(request) if request else None,
                "blocked": json.loads(blocked) if blocked else None}

    async def user_has_control(self, server_id: str) -> bool:
        return bool(await self.r.exists(self._dk(server_id, "control")))

    async def take_control(self, server_id: str, info: dict) -> None:
        # Expires after a day so a forgotten takeover cannot block an agent forever.
        await self.r.set(self._dk(server_id, "control"), json.dumps({**info, "since": _ts()}), ex=86400)

    async def mark_blocked(self, server_id: str, info: dict) -> None:
        await self.r.set(self._dk(server_id, "blocked"), json.dumps(info), ex=86400)

    async def set_request(self, server_id: str, info: dict, ttl: int) -> None:
        await self.r.delete(self._dk(server_id, "handback"))  # no stale hand-backs
        await self.r.set(self._dk(server_id, "request"), json.dumps({**info, "at": _ts()}), ex=ttl)

    async def release_control(self, server_id: str, note: str) -> dict:
        """Clear control and wake a waiting agent. Returns the state as it was."""
        state = await self.desktop_state(server_id)
        await self.r.delete(self._dk(server_id, "control"), self._dk(server_id, "request"),
                            self._dk(server_id, "blocked"))
        if state["request"]:
            key = self._dk(server_id, "handback")
            await self.r.rpush(key, json.dumps({"note": note, "at": _ts()}))
            await self.r.expire(key, 120)
        return state

    async def wait_handback(self, server_id: str, timeout: float) -> dict | None:
        got = await self.r.blpop([self._dk(server_id, "handback")], timeout=timeout)
        return json.loads(got[1]) if got else None

    async def clear_request(self, server_id: str) -> None:
        await self.r.delete(self._dk(server_id, "request"))

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
