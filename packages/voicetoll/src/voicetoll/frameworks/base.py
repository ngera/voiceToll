"""The contract every framework adapter follows, plus helpers they share.

An adapter is a module that:

1. Instruments one call through a single entry callable (`attach(...)`, `Observer(...)`), which creates a
   `CallSession(source=<framework name>, ...)` with tenant, call id and optional tags.
2. Owns its framework's naming convention: it reduces whatever the framework says about a provider
   (a plugin module path, a service class name, a metrics field) to a brand token and passes it through
   `voicetoll.providers.normalize_provider`. Framework-specific quirks stay in the adapter module; the
   canonical provider ids stay in `providers.py`, shared by all adapters.
3. Maps the framework's own metrics to `call.emit(component, units, ...)` using voice-prices unit names
   (`characters`, `audio_input_seconds`, `input_tokens`, ...) and timings in milliseconds.
4. Follows the hot-path rules: never blocks, never does I/O, never raises (catch everything, count it in
   `call.client.errors`), never imports the framework at module import time, never captures content.
5. Accepts `providers={component: (provider, model)}` so the app can override detection.

Tests: add duck-typed stand-ins to `tests/test_adapters.py` and, when the framework can be installed,
contract tests pinned to its version in `tests/test_contract.py`.
"""

from __future__ import annotations

from typing import Any

from ..providers import normalize_provider

COMPONENTS = ("stt", "llm", "tts", "s2s", "vad", "telephony", "platform", "turn")


def ms(seconds: Any) -> float | None:
    """Framework seconds -> milliseconds; None for missing, invalid or negative values."""
    if seconds is None:
        return None
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return None
    return value * 1000.0 if value >= 0 else None


def text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = ["COMPONENTS", "ms", "normalize_provider", "text_or_none"]
