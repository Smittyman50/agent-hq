"""In-process pub/sub so the UI sees what agents are doing, per thread."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, thread_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[thread_id].add(q)
        return q

    def unsubscribe(self, thread_id: str, q: asyncio.Queue) -> None:
        self._subs[thread_id].discard(q)

    def publish(self, thread_id: str, type_: str, **data: Any) -> None:
        event = {"type": type_, "thread_id": thread_id,
                 "ts": datetime.now(timezone.utc).isoformat(), **data}
        for q in list(self._subs.get(thread_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


bus = EventBus()
