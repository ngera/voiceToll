"""Vapi BYOK call importer (M4).

Vapi's costBreakdown shows $0 for bring-your-own-key provider spend. This importer
reads a Vapi call object (or list) and emits CaptureEvents for STT/LLM/TTS units so
voiceToll can price them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .ids_otlp import otlp_event_id


def _ts(value: Any) -> float:
    if value is None:
        return datetime.now(tz=UTC).timestamp()
    if isinstance(value, (int, float)):
        # Vapi often uses ms epoch
        return float(value) / 1000.0 if value > 1e12 else float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return datetime.now(tz=UTC).timestamp()


def call_to_events(call: dict[str, Any], *, project: str) -> list[dict[str, Any]]:
    call_id = str(call.get("id") or call.get("callId") or "vapi-unknown")
    tenant = call.get("orgId") or call.get("assistantId")
    started = _ts(call.get("startedAt") or call.get("createdAt"))
    events: list[dict[str, Any]] = []
    # Prefer explicit costBreakdown / artifact usage when present
    breakdown = call.get("costBreakdown") or {}
    analysis = call.get("analysis") or {}
    usage = call.get("usage") or analysis.get("usage") or {}

    def add(
        component: str, provider: str | None, model: str | None, units: dict[str, float], **extra: Any
    ) -> None:
        if not units:
            return
        event_id = otlp_event_id(call_id, component, json.dumps(units, sort_keys=True)[:32])
        events.append(
            {
                "event_id": event_id,
                "schema": 1,
                "source": "vapi-import",
                "ts": started,
                "project": project,
                "tenant": str(tenant) if tenant else None,
                "session": call_id,
                "component": component,
                "provider": (provider or "").lower() or None,
                "model": model,
                "units": units,
                "src": {k: "reported" for k in units},
                "how": {},
                "timing_ms": {},
                "status": "ok",
                "cancelled": False,
                "tags": {"feature": "vapi"},
                **extra,
            }
        )

    # Structured usage if present
    stt = usage.get("stt") or breakdown.get("stt") or {}
    llm = usage.get("llm") or breakdown.get("llm") or {}
    tts = usage.get("tts") or breakdown.get("tts") or {}

    if isinstance(stt, dict):
        seconds = stt.get("seconds") or stt.get("audio_input_seconds") or stt.get("duration")
        if seconds:
            add("stt", stt.get("provider"), stt.get("model"), {"audio_input_seconds": float(seconds)})
    if isinstance(llm, dict):
        units = {}
        if llm.get("promptTokens") or llm.get("input_tokens"):
            units["input_tokens"] = float(llm.get("promptTokens") or llm.get("input_tokens"))
        if llm.get("completionTokens") or llm.get("output_tokens"):
            units["output_tokens"] = float(llm.get("completionTokens") or llm.get("output_tokens"))
        add("llm", llm.get("provider") or "openai", llm.get("model"), units)
    if isinstance(tts, dict):
        chars = tts.get("characters") or tts.get("characterCount")
        if chars:
            add("tts", tts.get("provider"), tts.get("model"), {"characters": float(chars)})

    # Fallback: messages / artifact transcript length for TTS characters
    if not any(e["component"] == "tts" for e in events):
        messages = call.get("messages") or []
        chars = sum(
            len(m.get("message") or m.get("content") or "") for m in messages if m.get("role") == "bot"
        )
        if chars:
            add(
                "tts",
                "elevenlabs",
                None,
                {"characters": float(chars)},
                src={"characters": "estimated"},
                how={"characters": "vapi_bot_message_len"},
            )

    return events


def load_vapi_file(path: str | Path) -> list[dict[str, Any]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "calls" in data:
        return list(data["calls"])
    if isinstance(data, dict):
        return [data]
    raise ValueError("expected a Vapi call object, {calls: [...]}, or a list")


def import_vapi_calls(path: str | Path, *, project: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for call in load_vapi_file(path):
        events.extend(call_to_events(call, project=project))
    return events
