"""In-memory pub/sub per run (one asyncio.Queue per subscriber) for SSE.

Live streaming only — replay/persistence is store.events' job. A slow subscriber
(full queue) must never block the loop: the event is dropped, and it can recover
via `?after=<id>` replay since every event is persisted anyway.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

QUEUE_SIZE = 1000


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subs[run_id].add(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        self._subs[run_id].discard(q)
        if not self._subs[run_id]:
            del self._subs[run_id]

    def publish(self, run_id: str, event: dict) -> None:
        for q in list(self._subs.get(run_id, ())):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow subscriber → drop it; they replay from the DB

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))
