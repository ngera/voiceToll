"""Background exporter: drains the buffer and POSTs gzip JSON batches to the collector."""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .buffer import RingBuffer
    from .config import Config

log = logging.getLogger("voicetoll")


class Exporter:
    def __init__(
        self, config: Config, buffer: RingBuffer, stats: Callable[[], dict[str, Any]] | None = None
    ) -> None:
        self._config = config
        self._buffer = buffer
        self._stats = stats  # the client's own counters, sent with every batch (see _send)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._inflight = 0  # batches taken from the buffer and not yet acknowledged or requeued
        self._inflight_lock = threading.Lock()
        self.sent = 0
        self.failed_batches = 0
        self.dropped_after_retry = 0

    # ---- lifecycle -------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="voicetoll-exporter", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)
        self.flush(timeout)

    def nudge(self) -> None:
        self._wake.set()

    # ---- sending ---------------------------------------------------------------------------
    def flush(self, timeout: float = 5.0) -> bool:
        """Send everything currently buffered. Returns False if something could not be sent."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            batch = self._buffer.take(self._config.batch_size)
            if batch:
                if not self._send(batch):
                    self._buffer.requeue_front(batch)
                    return False
                continue
            if not self._inflight:  # the background thread may still be sending a batch it took
                break
            time.sleep(0.01)
        return len(self._buffer) == 0 and not self._inflight

    def _run(self) -> None:
        backoff = 0.0
        failing_since: float | None = None
        while not self._stop.is_set():
            self._wake.wait(timeout=self._config.flush_interval if backoff == 0 else backoff)
            self._wake.clear()
            while len(self._buffer):
                batch = self._buffer.take(self._config.batch_size)
                if not batch:
                    break
                self._track(1)  # until sent, requeued or dropped, so flush() waits for it
                try:
                    if self._send(batch):
                        backoff = 0.0
                        failing_since = None
                        continue
                    now = time.monotonic()
                    failing_since = failing_since or now
                    if now - failing_since > self._config.max_retry_seconds:
                        self.dropped_after_retry += len(batch)
                        self._buffer.dropped += len(batch)
                        failing_since = now
                    else:
                        self._buffer.requeue_front(batch)
                finally:
                    self._track(-1)
                backoff = min(10.0, max(1.0, backoff * 2))
                break

    def _track(self, delta: int) -> None:
        with self._inflight_lock:
            self._inflight += delta

    def _send(self, batch: list[dict]) -> bool:
        payload: dict[str, Any] = {"events": batch}
        if self._stats is not None:
            try:  # counters only (drops, errors, buffer depth): lets the collector show client health
                payload["client"] = self._stats()
            except Exception:
                pass
        body = gzip.compress(json.dumps(payload, separators=(",", ":"), default=str).encode())
        headers = {"Content-Type": "application/json", "Content-Encoding": "gzip"}
        if self._config.ingest_key:
            headers["X-Voicetoll-Key"] = self._config.ingest_key
        url = self._config.endpoint.rstrip("/") + "/v1/events"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._send_lock, urllib.request.urlopen(req, timeout=self._config.request_timeout) as resp:
                ok = 200 <= resp.status < 300
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("voicetoll export failed: %s", exc)
            ok = False
        if ok:
            self.sent += len(batch)
        else:
            self.failed_batches += 1
        return ok
