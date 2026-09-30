"""Stable event ids for OTLP spans (stdlib only; no voicetoll client dependency)."""

from __future__ import annotations

import hashlib


def otlp_event_id(session: str, span_id: str, component: str) -> str:
    digest = hashlib.sha256(f"{session}|{span_id}|{component}".encode()).hexdigest()[:24]
    return f"otlp_{digest}"
