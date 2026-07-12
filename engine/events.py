"""In-memory pub/sub per run (asyncio.Queue per subscriber) buat SSE.

Live-stream doang — replay/persist urusan store.events. Subscriber lelet
(queue penuh) nggak boleh nge-block loop: event di-drop, dia bisa recover
lewat replay `?after=<id>` karena semua event toh ke-persist.
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
                pass  # subscriber lelet → drop; dia replay dari DB

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subs.get(run_id, ()))
