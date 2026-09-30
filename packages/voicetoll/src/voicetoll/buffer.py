"""Bounded, drop-newest event buffer.

`put` is O(1) and never blocks: when the buffer is full the new event is dropped and counted.
`deque.append` and `popleft` are atomic in CPython, so no lock is needed on the hot path.
"""

from __future__ import annotations

from collections import deque
from typing import Any


class RingBuffer:
    __slots__ = ("_items", "_capacity", "dropped")

    def __init__(self, capacity: int) -> None:
        self._items: deque[dict[str, Any]] = deque()
        self._capacity = capacity
        self.dropped = 0

    def put(self, item: dict[str, Any]) -> bool:
        if len(self._items) >= self._capacity:
            self.dropped += 1
            return False
        self._items.append(item)
        return True

    def take(self, max_items: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        items = self._items
        while items and len(out) < max_items:
            try:
                out.append(items.popleft())
            except IndexError:  # raced with another consumer
                break
        return out

    def requeue_front(self, batch: list[dict[str, Any]]) -> int:
        """Put a failed batch back at the front, as far as capacity allows. Returns how many were dropped."""
        room = self._capacity - len(self._items)
        keep = batch[:room] if room > 0 else []
        for item in reversed(keep):
            self._items.appendleft(item)
        lost = len(batch) - len(keep)
        self.dropped += lost
        return lost

    def __len__(self) -> int:
        return len(self._items)
