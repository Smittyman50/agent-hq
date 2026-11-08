"""Provider slots: a limit on how many model requests run against one provider at once.

Set per provider on the Models page ("Max concurrent requests", 0 = no limit). The limit
holds across every worker and machine: slots are leases in a Valkey sorted set
(ahq:slots:<provider_id>, member = lease token, score = lease expiry). A lease is renewed
while its request runs and simply expires if the worker holding it dies, so a crash can
never leak a slot for longer than LEASE_SECONDS.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.models.wrapper import WrapperModel

LEASE_SECONDS = 90
RENEW_EVERY = 30
POLL_SECONDS = 0.25

_client = None  # redis.asyncio client, set by the Runtime of each process

# Drop expired leases, then take a slot if one is free. Atomic, so two workers can't both
# take the last slot.
_ACQUIRE = """
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[4])
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]) * 2)
  return 1
end
return 0
"""


def use(client) -> None:
    global _client
    _client = client


def key(provider_id: str) -> str:
    return f"ahq:slots:{provider_id}"


async def in_use(provider_id: str) -> int:
    if _client is None:
        return 0
    await _client.zremrangebyscore(key(provider_id), "-inf", time.time())
    return await _client.zcard(key(provider_id))


@contextlib.asynccontextmanager
async def slot(provider_id: str, limit: int, timeout: float | None = None) -> AsyncIterator[None]:
    """Hold one of the provider's `limit` slots for the duration of the block."""
    if limit <= 0 or _client is None:
        yield
        return
    token = uuid.uuid4().hex
    k = key(provider_id)
    deadline = time.monotonic() + timeout if timeout else None
    while not await _client.eval(_ACQUIRE, 1, k, time.time(), limit, LEASE_SECONDS, token):
        if deadline and time.monotonic() > deadline:
            raise TimeoutError(f"no free slot on this provider after {timeout:.0f}s")
        await asyncio.sleep(POLL_SECONDS)

    async def renew() -> None:
        while True:
            await asyncio.sleep(RENEW_EVERY)
            with contextlib.suppress(Exception):
                await _client.zadd(k, {token: time.time() + LEASE_SECONDS}, xx=True)

    keeper = asyncio.create_task(renew())
    try:
        yield
    finally:
        keeper.cancel()
        with contextlib.suppress(Exception):
            await _client.zrem(k, token)


class SlotLimitedModel(WrapperModel):
    """Takes a provider slot for each model request (streamed or not)."""

    def __init__(self, wrapped: Any, provider_id: str, limit: int) -> None:
        super().__init__(wrapped)
        self.provider_id = provider_id
        self.limit = limit

    async def request(self, *args: Any, **kwargs: Any):
        async with slot(self.provider_id, self.limit):
            return await self.wrapped.request(*args, **kwargs)

    @contextlib.asynccontextmanager
    async def request_stream(self, *args: Any, **kwargs: Any):
        async with slot(self.provider_id, self.limit):
            async with self.wrapped.request_stream(*args, **kwargs) as stream:
                yield stream
