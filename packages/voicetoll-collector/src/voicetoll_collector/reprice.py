"""Repricer: regenerate cost_line rows under a new price / rate-card version (M4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .pricing import Pricer
from .schema import CaptureEvent
from .store import Store


def event_from_row(row: dict[str, Any]) -> CaptureEvent:
    units_raw = json.loads(row["units_json"] or "{}")
    units = {k: float(v["v"]) for k, v in units_raw.items()}
    src = {k: v.get("src", "reported") for k, v in units_raw.items()}
    how = {k: v.get("how") for k, v in units_raw.items() if v.get("how")}
    timing_ms = {}
    for col, key in (
        ("ttfb_ms", "ttfb"),
        ("ttft_ms", "ttft"),
        ("duration_ms", "duration"),
        ("eou_delay_ms", "eou_delay"),
        ("transcription_delay_ms", "transcription_delay"),
        ("processing_ms", "processing"),
    ):
        if row.get(col) is not None:
            timing_ms[key] = float(row[col])
    tags = {}
    for key in ("feature", "agent_version", "env", "region", "caller_country"):
        if row.get(key):
            tags[key] = row[key]
    return CaptureEvent.model_validate(
        {
            "event_id": row["event_id"],
            "schema": 1,
            "source": row.get("source") or "sdk",
            "ts": datetime.fromtimestamp(float(row["ts_epoch"]), tz=UTC),
            "project": row["project_id"],
            "tenant": row.get("tenant_id"),
            "user": row.get("user_id"),
            "session": row["session_id"],
            "turn": row.get("turn"),
            "component": row["component"],
            "provider": row.get("provider"),
            "model": row.get("model"),
            "voice_class": row.get("voice_class"),
            "units": units,
            "src": src,
            "how": how,
            "timing_ms": timing_ms,
            "status": row.get("status") or "ok",
            "cancelled": bool(row.get("cancelled")),
            "request_id": row.get("request_id"),
            "tags": tags,
        }
    )


def reprice(
    store: Store,
    pricer: Pricer,
    project: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    since_day: str | None = None,
) -> dict[str, int]:
    rows = store.list_events_for_reprice(project, provider=provider, model=model, since_day=since_day)
    updated = 0
    sessions: set[tuple[str, str]] = set()
    for row in rows:
        event = event_from_row(row)
        lines = pricer.price(event)
        store.supersede_cost_lines(event.event_id, lines)
        updated += 1
        sessions.add((project, event.session))
    for proj, session in sessions:
        store.refresh_call_rollups(proj, session)
    store.flush_cost_days()
    return {"events": updated, "sessions": len(sessions)}
