"""Ingest pipeline: validate/scrub -> price -> store, with a disk spool when the store is down."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from pydantic import ValidationError

from .pricing import Pricer
from .schema import CaptureEvent
from .spool import Spool
from .store import Store

log = logging.getLogger("voicetoll.collector")


class Pipeline:
    def __init__(self, store: Store, pricer: Pricer, spool: Spool) -> None:
        self.store = store
        self.pricer = pricer
        self.spool = spool
        self.counters: Counter[str] = Counter()

    def ingest(self, raw_events: list[Any], project: str | None) -> dict[str, Any]:
        """Validate a batch and store it (or spool it). Raises SpoolFull if neither is possible."""
        valid: list[CaptureEvent] = []
        rejected = 0
        for raw in raw_events:
            if not isinstance(raw, dict):
                rejected += 1
                continue
            if project is not None:
                raw = {**raw, "project": project}  # the ingest key decides the project, not the payload
            try:
                valid.append(CaptureEvent.model_validate(raw))
            except ValidationError:
                rejected += 1
        self.counters["events_rejected"] += rejected
        result: dict[str, Any] = {
            "accepted": len(valid),
            "rejected": rejected,
            "duplicates": 0,
            "spooled": False,
        }
        if not valid:
            return result
        try:
            stored = self.process(valid)
            result["duplicates"] = stored["duplicates"]
        except Exception as exc:
            log.warning("store unavailable, spooling %d events: %s", len(valid), exc)
            self.spool.write([e.model_dump(mode="json", by_alias=True) for e in valid])  # may raise SpoolFull
            self.counters["events_spooled"] += len(valid)
            result["spooled"] = True
        self.counters["events_accepted"] += len(valid)
        return result

    def process(self, events: list[CaptureEvent]) -> dict[str, int]:
        items = []
        for event in events:
            lines = self.pricer.price(event)
            self.counters["cost_lines"] += len(lines)
            self.counters["cost_lines_unpriced"] += sum(
                1 for line in lines if line.price_source == "unpriced"
            )
            items.append((event, lines))
        result = self.store.insert_batch(items)
        self.counters["events_stored"] += result.inserted
        self.counters["events_duplicate"] += result.duplicates
        return {"inserted": result.inserted, "duplicates": result.duplicates}

    def replay_spool(self) -> int:
        def _process(raw: list[dict[str, Any]]) -> None:
            events = []
            for item in raw:
                try:
                    events.append(CaptureEvent.model_validate(item))
                except ValidationError:  # e.g. written by an older collector; drop it, keep the rest
                    self.counters["events_rejected"] += 1
            if events:
                self.process(events)

        replayed = self.spool.replay(_process, storage_ok=self.store.ping)
        self.counters["events_replayed"] += replayed
        return replayed
