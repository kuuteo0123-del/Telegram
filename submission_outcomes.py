"""Domain queue manager with per-domain backpressure."""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class DomainQueueItem:
    domain: str
    code: str
    account: str
    item_id: int | None = None
    retries: int = 0


class DomainQueueManager:
    def __init__(self, max_per_domain: int = 500):
        self.max_per_domain = max_per_domain
        self._queues: dict[str, deque[DomainQueueItem]] = defaultdict(deque)
        self._lock = threading.RLock()

    def push(self, item: DomainQueueItem) -> None:
        with self._lock:
            q = self._queues[item.domain]
            if len(q) >= self.max_per_domain:
                raise OverflowError(f"domain queue full: {item.domain}")
            q.append(item)

    def pop(self, domain: str) -> DomainQueueItem | None:
        with self._lock:
            q = self._queues.get(domain)
            if not q:
                return None
            return q.popleft() if q else None

    def size(self, domain: str | None = None) -> int:
        with self._lock:
            if domain:
                return len(self._queues.get(domain, deque()))
            return sum(len(q) for q in self._queues.values())

    def clear(self) -> None:
        with self._lock:
            self._queues.clear()


__all__ = ["DomainQueueItem", "DomainQueueManager"]


# queue_manager.py
