"""Price upkeep: flag stale list prices, pick up rate-card and price changes, and re-cost history (M4).

Two jobs, both run by the collector on a timer and both safe to run by hand:

- `check_price_freshness` looks at every provider/model that carried cost in the last 7 days and records
  how trustworthy its voice-prices rate is today (verified, stale, imported or seed). Spend covered by a
  rate card is shown separately, since your own contracted rate does not go stale with the catalog.
- `apply_pricing_changes` reloads the rate-card file when it changes and, when the rate-card version or
  the voice-prices version differs from the one history was priced with, re-costs recent events. Events
  whose cost comes out the same are left alone, so only real changes create new cost-line versions.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from .pricing import RateCards
from .reprice import event_from_row

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .pricing import Pricer
    from .settings import Settings
    from .store import Store

log = logging.getLogger("voicetoll.collector.upkeep")

VERSIONS_KEY = "pricing_versions"
LAST_REPRICE_KEY = "last_auto_reprice"
RATE_CARD_ERROR_KEY = "rate_card_error"


# ---- price freshness --------------------------------------------------------------------------
def check_price_freshness(store: Store, pricer: Pricer, project: str, *, days: int = 7) -> list[dict[str, Any]]:
    today = datetime.now(tz=UTC).date()
    since = (today - timedelta(days=days - 1)).isoformat()
    per_model: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.spend_by_model(project, since):
        key = (row["provider"], row["model"])
        agg = per_model.setdefault(key, {"list_spend_usd": 0.0, "rate_card_spend_usd": 0.0, "events": 0})
        if row["price_source"] == "voice_prices":
            agg["list_spend_usd"] += row["cost_usd"]
        elif row["price_source"] == "rate_card":
            agg["rate_card_spend_usd"] += row["cost_usd"]
        agg["events"] = max(agg["events"], int(row["events"] or 0))
    out = []
    for (provider, model), agg in per_model.items():
        fresh = pricer.list_price_freshness(provider, model, today)
        if fresh is None:
            status = "rate_card" if agg["rate_card_spend_usd"] > 0 else "not_in_catalog"
            fresh = {"status": status, "confidence": None, "last_verified": None, "age_days": None,
                     "threshold_days": None}
        elif agg["list_spend_usd"] == 0 and agg["rate_card_spend_usd"] > 0:
            fresh = {**fresh, "status": "rate_card"}  # your own rate applies; the catalog's age does not matter
        out.append(
            {
                "provider": provider,
                "model": model,
                **fresh,
                "list_spend_usd": round(agg["list_spend_usd"], 8),
                "rate_card_spend_usd": round(agg["rate_card_spend_usd"], 8),
                "events": agg["events"],
                "checked_day": today.isoformat(),
                "checked_epoch": time.time(),
            }
        )
    store.save_price_freshness(project, out)
    return out


# ---- repricing when prices change -----------------------------------------------------------------
def _same_cost(old: list[dict[str, Any]], new: list[Any]) -> bool:
    def key(meter: str, amount: Any, source: str) -> tuple[str, str, str]:
        return (meter, f"{Decimal(str(amount)):.10f}", source)

    return sorted(key(r["meter"], r["amount_usd"], r["price_source"]) for r in old) == sorted(
        key(line.meter, line.amount_usd, line.price_source) for line in new
    )


def reprice_changed(store: Store, pricer: Pricer, project: str, since_day: str) -> dict[str, Any]:
    """Re-cost events since a day; only events whose cost lines actually change get a new version."""
    rows = store.list_events_for_reprice(project, since_day=since_day)
    current = store.current_lines_by_event([r["event_id"] for r in rows])
    changed = 0
    delta = Decimal(0)
    sessions: set[str] = set()
    for row in rows:
        event = event_from_row(row)
        lines = pricer.price(event)
        old = current.get(event.event_id, [])
        if _same_cost(old, lines):
            continue
        delta += sum((line.amount_usd for line in lines), Decimal(0)) - sum(
            (Decimal(str(r["amount_usd"])) for r in old), Decimal(0)
        )
        store.supersede_cost_lines(event.event_id, lines)
        changed += 1
        sessions.add(event.session)
    for session in sessions:
        store.refresh_call_rollups(project, session)
    store.flush_cost_days()
    return {"events_checked": len(rows), "events_changed": changed, "sessions": len(sessions), "delta_usd": float(delta)}


def apply_pricing_changes(pipeline: Pipeline, settings: Settings) -> dict[str, Any] | None:
    """Reload rate cards if the file changed; reprice recent history when price versions changed."""
    store, pricer = pipeline.store, pipeline.pricer
    if settings.rate_cards_path:
        try:
            cards = RateCards.load(settings.rate_cards_path)
        except Exception as exc:  # a half-saved or invalid YAML must not take pricing down
            message = f"{type(exc).__name__}: {exc}"
            if store.get_state(RATE_CARD_ERROR_KEY) != message:
                log.error("rate card file not loaded, keeping the previous rates: %s", message)
                store.set_state(RATE_CARD_ERROR_KEY, message)
            cards = None
        if cards is not None:
            if store.get_state(RATE_CARD_ERROR_KEY):
                store.set_state(RATE_CARD_ERROR_KEY, "")
            if cards.version != pricer.rate_cards.version:
                log.info("rate cards changed: %s -> %s", pricer.rate_cards.version or "(none)", cards.version)
                pricer.rate_cards = cards  # new events use the new rates from here on

    versions = {"rate_card_version": pricer.rate_cards.version, "price_version": pricer.price_version}
    previous = store.get_state(VERSIONS_KEY)
    if previous == versions:
        return None
    store.set_state(VERSIONS_KEY, versions)
    if previous is None or settings.auto_reprice_days <= 0:
        return None  # first run records the baseline; repricing is off when the window is 0

    since = (datetime.now(tz=UTC) - timedelta(days=settings.auto_reprice_days)).strftime("%Y-%m-%d")
    reason = [k for k in versions if versions[k] != previous.get(k)]
    summary: dict[str, Any] = {
        "at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "reason": reason,
        "from": previous,
        "to": versions,
        "since_day": since,
        "projects": {},
    }
    for project in store.projects_with_events(since):
        summary["projects"][project] = reprice_changed(store, pricer, project, since)
    store.set_state(LAST_REPRICE_KEY, summary)
    log.info("automatic reprice after %s change: %s", ", ".join(reason), summary["projects"])
    return summary
