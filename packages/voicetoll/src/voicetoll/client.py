"""The client: builds capture events and enqueues them. Nothing here blocks or raises into the app."""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any

from .buffer import RingBuffer
from .config import Config
from .exporter import Exporter
from .ids import new_event_id

try:
    from importlib.metadata import version as _dist_version

    SDK_VERSION = _dist_version("voicetoll")
except Exception:  # running from source
    SDK_VERSION = "0.1.0.dev0"

log = logging.getLogger("voicetoll")

SCHEMA_VERSION = 1
COMPONENTS = frozenset({"stt", "llm", "tts", "s2s", "vad", "telephony", "platform", "turn"})
UNIT_NAMES = frozenset(
    {
        "characters",
        "audio_input_seconds",
        "audio_output_seconds",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "input_audio_tokens",
        "output_audio_tokens",
        "cache_audio_read_tokens",
        "agent_minutes",
        "telephony_minutes",
    }
)
TIMING_NAMES = frozenset({"ttfb", "ttft", "duration", "eou_delay", "transcription_delay", "processing"})


class VoiceToll:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or Config.from_env()
        self.buffer = RingBuffer(self.config.buffer_size)
        self.client_id = new_event_id()  # random per process; identifies this client in collector health
        self.started_epoch = time.time()
        self.exporter = Exporter(self.config, self.buffer, stats=self.wire_stats)
        self._started = False
        self._start_lock = threading.Lock()
        self.errors = 0
        self.enqueued = 0

    # ---- hot path --------------------------------------------------------------------------
    def enqueue(self, event: dict[str, Any]) -> bool:
        """Put one event in the buffer. O(1), never blocks, never raises."""
        try:
            if self.config.disabled:
                return False
            ok = self.buffer.put(event)
            if ok:
                self.enqueued += 1
                buffered = len(self.buffer)
                if not self._started:
                    self._start()
                elif buffered > 0 and buffered % self.config.batch_size == 0:
                    self.exporter.nudge()
            return ok
        except Exception:  # fail open
            self.errors += 1
            return False

    def _start(self) -> None:
        with self._start_lock:
            if not self._started:
                self.exporter.start()
                self._started = True

    # ---- event building --------------------------------------------------------------------
    def build_event(
        self,
        component: str,
        provider: str | None,
        model: str | None,
        units: dict[str, float | int | None] | None = None,
        *,
        session_id: str,
        tenant: str | None = None,
        user: str | None = None,
        turn: int | None = None,
        source: str = "sdk",
        src: dict[str, str] | str | None = None,
        how: dict[str, str] | None = None,
        timing_ms: dict[str, float | None] | None = None,
        status: str = "ok",
        cancelled: bool = False,
        request_id: str | None = None,
        voice_class: str | None = None,
        tags: dict[str, str | None] | None = None,
        ts: float | None = None,
    ) -> dict[str, Any]:
        clean_units = {k: v for k, v in (units or {}).items() if v is not None and k in UNIT_NAMES and v != 0}
        if isinstance(src, str) or src is None:
            default_src = src or "reported"
            src_map = {k: default_src for k in clean_units}
        else:
            src_map = {k: src.get(k, "reported") for k in clean_units}
        event: dict[str, Any] = {
            "event_id": new_event_id(),
            "schema": SCHEMA_VERSION,
            "source": source,
            "ts": ts if ts is not None else time.time(),
            "project": self.config.project,
            "tenant": tenant,
            "user": user,
            "session": session_id,
            "turn": turn,
            "component": component,
            "provider": provider,
            "model": model,
            "voice_class": voice_class,
            "units": clean_units,
            "src": src_map,
            "status": status,
            "cancelled": bool(cancelled),
        }
        if how:
            event["how"] = {k: v for k, v in how.items() if k in clean_units}
        if timing_ms:
            timing = {
                k: round(float(v), 3) for k, v in timing_ms.items() if v is not None and k in TIMING_NAMES
            }
            if timing:
                event["timing_ms"] = timing
        if request_id:
            event["request_id"] = str(request_id)[:128]
        merged_tags: dict[str, str] = {}
        if self.config.env:
            merged_tags["env"] = self.config.env
        if self.config.region:
            merged_tags["region"] = self.config.region
        merged_tags.update(self.config.extra_tags)
        if tags:
            merged_tags.update({k: str(v) for k, v in tags.items() if v is not None})
        if merged_tags:
            event["tags"] = merged_tags
        return event

    def record(self, component: str, provider: str | None, model: str | None, units=None, **kwargs) -> bool:
        """Build and enqueue one event. Use for apps without a framework adapter."""
        try:
            return self.enqueue(self.build_event(component, provider, model, units, **kwargs))
        except Exception:
            self.errors += 1
            return False

    # ---- control ---------------------------------------------------------------------------
    def flush(self, timeout: float = 5.0) -> bool:
        return self.exporter.flush(timeout)

    def shutdown(self, timeout: float = 5.0) -> None:
        if self._started:
            self.exporter.stop(timeout)

    def wire_stats(self) -> dict[str, Any]:
        """Counters sent to the collector with each batch. Numbers and ids only, never event content."""
        return {
            "client_id": self.client_id,
            "sdk_version": SDK_VERSION,
            "dropped": self.buffer.dropped,
            "errors": self.errors,
            "sent": self.exporter.sent,
            "buffer_len": len(self.buffer),
            "buffer_max": self.config.buffer_size,
            "started_epoch": round(self.started_epoch, 3),
        }

    def stats(self) -> dict[str, int]:
        return {
            "enqueued": self.enqueued,
            "buffered": len(self.buffer),
            "sent": self.exporter.sent,
            "dropped": self.buffer.dropped,
            "failed_batches": self.exporter.failed_batches,
            "errors": self.errors,
        }


# ---- module-level singleton ----------------------------------------------------------------
_client: VoiceToll | None = None
_client_lock = threading.Lock()


def get_client() -> VoiceToll:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = VoiceToll()
    return _client


def configure(**overrides: Any) -> VoiceToll:
    """Configure the global client. Unset values fall back to VOICETOLL_* environment variables."""
    global _client
    with _client_lock:
        if _client is not None:
            _client.shutdown(timeout=1.0)
        _client = VoiceToll(Config.from_env().with_overrides(**overrides))
    return _client


def record(component: str, provider: str | None, model: str | None, units=None, **kwargs) -> bool:
    return get_client().record(component, provider, model, units, **kwargs)


def flush(timeout: float = 5.0) -> bool:
    return get_client().flush(timeout)


def shutdown(timeout: float = 5.0) -> None:
    if _client is not None:
        _client.shutdown(timeout)


def stats() -> dict[str, int]:
    return get_client().stats()


atexit.register(lambda: shutdown(timeout=2.0))
