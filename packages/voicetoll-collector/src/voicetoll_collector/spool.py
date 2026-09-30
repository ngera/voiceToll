"""Append-only disk spool used when the database is unavailable.

Each failed batch becomes one JSONL file. `replay` processes files oldest first and deletes each one
only after it was stored; replays are safe because inserts are idempotent on event_id.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("voicetoll.collector")


class SpoolFull(Exception):
    pass


class Spool:
    def __init__(self, directory: str, max_bytes: int) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.dir.glob("spool-*.jsonl"))

    def pending(self) -> list[Path]:
        return sorted(self.dir.glob("spool-*.jsonl"))

    def write(self, events: list[dict[str, Any]]) -> None:
        payload = "".join(json.dumps(e, separators=(",", ":"), default=str) + "\n" for e in events).encode()
        with self._lock:
            if self.size_bytes() + len(payload) > self.max_bytes:
                raise SpoolFull(f"spool at {self.dir} is full ({self.max_bytes} bytes)")
            final = self.dir / f"spool-{time.time_ns():020d}.jsonl"
            tmp = final.with_suffix(".tmp")
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)

    def rejected(self) -> list[Path]:
        return sorted((self.dir / "rejected").glob("spool-*.jsonl"))

    def replay(
        self,
        process: Callable[[list[dict[str, Any]]], None],
        *,
        storage_ok: Callable[[], bool] | None = None,
        max_failures: int = 3,
    ) -> int:
        """Process spooled batches in order. Stops at the first failure. Returns events replayed.

        A file that keeps failing while storage is healthy is not a database outage but a bad batch: after
        `max_failures` such attempts it moves to `rejected/`, so it cannot hold back the batches behind it.
        """
        replayed = 0
        with self._lock:
            for path in self.pending():
                try:
                    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
                    process(events)
                except Exception as exc:
                    healthy = storage_ok() if storage_ok is not None else False
                    count = self._failures.get(path.name, 0) + (1 if healthy else 0)
                    self._failures[path.name] = count
                    if healthy and count >= max_failures:
                        target = self.dir / "rejected"
                        target.mkdir(exist_ok=True)
                        os.replace(path, target / path.name)
                        self._failures.pop(path.name, None)
                        log.error(
                            "spool file %s failed %d replays with storage up; moved to rejected/: %s",
                            path.name,
                            count,
                            exc,
                        )
                        continue
                    log.warning("spool replay stopped at %s: %s", path.name, exc)
                    break
                path.unlink()
                self._failures.pop(path.name, None)
                replayed += len(events)
        return replayed
