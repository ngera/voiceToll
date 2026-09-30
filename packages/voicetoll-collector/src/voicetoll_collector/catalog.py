"""Every price the collector can apply: the voice-prices catalog plus rate-card entries.

The Prices view shows the prices your dollars rest on; this is the other half, the choices. Each row is one
provider · model with its current list prices in the units people compare (per 1M tokens, per 1K characters,
per minute of audio), the voiceToll meter each price applies to, freshness, and whether it is in use or
overridden by a rate card. Read-only: nothing here changes pricing.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from .pricing import RateCards

log = logging.getLogger("voicetoll.collector.catalog")

# voice-prices ModelPrice field -> (voiceToll meter, label, factor to the display unit, display unit)
PRICE_FIELDS: tuple[tuple[str, str, str, Decimal, str], ...] = (
    ("input_mtok", "input_tokens", "Input tokens", Decimal(1), "per 1M tokens"),
    ("cache_read_mtok", "cache_read_tokens", "Cached input tokens", Decimal(1), "per 1M tokens"),
    ("cache_write_mtok", "cache_write_tokens", "Cache write tokens", Decimal(1), "per 1M tokens"),
    ("output_mtok", "output_tokens", "Output tokens", Decimal(1), "per 1M tokens"),
    ("input_audio_mtok", "input_audio_tokens", "Input audio tokens", Decimal(1), "per 1M tokens"),
    ("cache_audio_read_mtok", "cache_audio_read_tokens", "Cached audio tokens", Decimal(1), "per 1M tokens"),
    ("output_audio_mtok", "output_audio_tokens", "Output audio tokens", Decimal(1), "per 1M tokens"),
    ("input_kchars", "characters", "Characters", Decimal(1), "per 1K characters"),
    ("input_audio_kseconds", "audio_input_seconds", "Audio in", Decimal(60) / Decimal(1000), "per minute"),
    ("output_audio_kseconds", "audio_output_seconds", "Audio out", Decimal(60) / Decimal(1000), "per minute"),
    ("agent_kminutes", "agent_minutes", "Agent time", Decimal(1) / Decimal(1000), "per minute"),
    ("telephony_kminutes", "telephony_minutes", "Telephony", Decimal(1) / Decimal(1000), "per minute"),
    ("requests_kcount", "requests", "Requests", Decimal(1), "per 1K requests"),
)

KINDS = ("llm", "stt", "tts", "s2s", "platform", "telephony", "other")

_cache: dict[str, Any] = {}


def _current_prices(model: Any, now: datetime) -> tuple[Any | None, bool]:
    """The ModelPrice in force now, and whether the model has conditional (dated or time-of-day) prices."""
    prices = model.prices
    if not isinstance(prices, list):
        return prices, False
    active = None
    for cond in prices:
        constraint = getattr(cond, "constraint", None)
        try:
            ok = constraint is None or constraint.active(now)
        except Exception:
            ok = False
        if ok:
            active = cond.prices  # the last active condition wins
    return active, True


def _price_value(raw: Any) -> tuple[Decimal | None, bool]:
    """(base price, tiered?) for a plain Decimal or a TieredPrices."""
    if raw is None:
        return None, False
    if isinstance(raw, Decimal | int | float):
        return Decimal(str(raw)), False
    base = getattr(raw, "base", None)
    return (Decimal(str(base)) if base is not None else None), True


def _kind(meters: set[str]) -> str:
    if meters & {"input_audio_tokens", "output_audio_tokens"}:
        return "s2s"
    if "characters" in meters or ("audio_output_seconds" in meters and "audio_input_seconds" not in meters):
        return "tts"
    if "audio_input_seconds" in meters:
        return "stt"
    if meters & {"input_tokens", "output_tokens"}:
        return "llm"
    if "agent_minutes" in meters:
        return "platform"
    if "telephony_minutes" in meters:
        return "telephony"
    return "other"


def _catalog_rows(today: date) -> tuple[list[dict[str, Any]], str]:
    import voice_prices
    from voice_prices.confidence import model_freshness
    from voice_prices.data_snapshot import get_snapshot

    version = str(getattr(voice_prices, "__version__", "unknown"))
    now = datetime.now(tz=UTC)
    rows: list[dict[str, Any]] = []
    for prov in get_snapshot().providers:
        for model in prov.models:
            current, conditional = _current_prices(model, now)
            prices = []
            for fname, meter, label, factor, unit in PRICE_FIELDS:
                value, tiered = _price_value(getattr(current, fname, None) if current is not None else None)
                if value is None:
                    continue
                prices.append(
                    {
                        "meter": meter,
                        "label": label,
                        "price": float(value * factor),
                        "unit": unit,
                        "tiered": tiered,
                    }
                )
            multipliers = getattr(current, "voice_multipliers", None) if current is not None else None
            try:
                fresh = model_freshness(model, prov, today=today)
                freshness = {
                    "status": fresh.verification_status,
                    "last_verified": fresh.last_verified.isoformat() if fresh.last_verified else None,
                    "age_days": fresh.age_days,
                }
            except Exception:
                freshness = None
            rows.append(
                {
                    "provider": prov.id,
                    "provider_name": prov.name,
                    "pricing_tier": getattr(prov, "pricing_tier", None),
                    "model": model.id,
                    "name": model.name,
                    "kind": _kind({p["meter"] for p in prices}),
                    "prices": prices,
                    "voice_classes": {k: float(v) for k, v in multipliers.items()} if multipliers else None,
                    "conditional": conditional,
                    "free": bool(getattr(model, "free", False)),
                    "deprecated": bool(getattr(model, "deprecated", False)),
                    "context_window": getattr(model, "context_window", None),
                    "freshness": freshness,
                    "source_url": getattr(model, "pricing_source_url", None)
                    or (prov.pricing_urls or [None])[0],
                    "origin": "voice_prices",
                }
            )
    return rows, version


def catalog_view(
    pipeline: Any, days: int = 30, filters: dict[str, str] | None = None, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """All prices available, marked with what is in use and what a rate card overrides, filtered and paged
    (the catalog has well over a thousand models, so the browser gets one page at a time)."""
    today = datetime.now(tz=UTC).date()
    pricer = pipeline.pricer
    key = f"{pricer.price_version}|{today.isoformat()}"
    if _cache.get("key") != key:
        try:
            rows, version = _catalog_rows(today)
            _cache.update(key=key, rows=rows, version=version, error=None)
        except Exception as exc:  # the catalog is a convenience; it must never break the admin UI
            log.warning("voice-prices catalog not listed: %s", exc)
            _cache.update(key=key, rows=[], version="unknown", error=f"{type(exc).__name__}")
    rows = [dict(r) for r in _cache["rows"]]
    by_pair = {(r["provider"], r["model"]): r for r in rows}

    # In use: provider/model pairs seen recently, mapped to catalog ids (apps often send dated model aliases)
    since = datetime.now(tz=UTC).timestamp() - days * 86_400
    used = pipeline.store._query(
        "SELECT provider, model, COUNT(*) AS events FROM usage_event WHERE ts_epoch >= ? AND provider IS NOT NULL "
        "GROUP BY provider, model",
        (since,),
    )
    in_use: dict[tuple[str, str], list[str]] = {}
    unmatched = []
    for u in used:
        pair = _match(u["provider"], u["model"])
        if pair in by_pair:
            in_use.setdefault(pair, []).append(u["model"] or "")
        elif u["model"]:
            unmatched.append({"provider": u["provider"], "model": u["model"], "events": int(u["events"])})
    for pair, sent in in_use.items():
        by_pair[pair]["in_use"] = True
        by_pair[pair]["sent_as"] = sorted({s for s in sent if s and s != pair[1]})

    rows += _rate_card_rows(pricer.rate_cards, by_pair)
    for r in rows:
        r.setdefault("in_use", False)
        r.setdefault("sent_as", [])
        r.setdefault("rate_card", [])
    matched = [r for r in rows if _keep(r, filters or {})]
    # in use first, then rate-card overrides, then by provider and model
    matched.sort(key=lambda r: (not r["in_use"], not r["rate_card"], r["provider"], r["model"]))
    provider_counts: dict[str, list[Any]] = {}
    for r in rows:
        entry = provider_counts.setdefault(r["provider"], [r.get("provider_name") or r["provider"], 0])
        entry[1] += 1
    kind_counts = {k: 0 for k in KINDS}
    for r in matched:
        kind_counts[r["kind"]] = kind_counts.get(r["kind"], 0) + 1
    return {
        "total": len(rows),
        "matched": len(matched),
        "offset": offset,
        "limit": limit,
        "rows": matched[offset : offset + limit],
        "providers": [{"id": k, "name": v[0], "models": v[1]} for k, v in sorted(provider_counts.items())],
        "kinds": kind_counts,
        "in_use_count": sum(1 for r in rows if r["in_use"]),
        "unmatched_in_use": unmatched,
        "versions": {"voice_prices": _cache["version"], "rate_card_version": pricer.rate_cards.version},
        "error": _cache.get("error"),
        "days": days,
    }


STATUSES = ("verified", "stale", "imported", "seed")


def _keep(row: dict[str, Any], f: dict[str, str]) -> bool:
    q = (f.get("q") or "").strip().lower()
    if (
        q
        and q
        not in " ".join(
            str(x or "") for x in (row["provider"], row.get("provider_name"), row["model"], row.get("name"))
        ).lower()
    ):
        return False
    if f.get("provider") and row["provider"] != f["provider"]:
        return False
    if f.get("kind") and row["kind"] != f["kind"]:
        return False
    if f.get("meter") and not any(p["meter"] == f["meter"] for p in row["prices"]):
        return False
    status = f.get("status")
    if status and (row.get("freshness") or {}).get("status") != status:
        return False
    if f.get("in_use") == "1" and not row["in_use"]:
        return False
    if f.get("rate_card") == "1" and not row["rate_card"]:
        return False
    if f.get("deprecated") != "1" and row["deprecated"]:
        return False
    if f.get("free") == "0" and row["free"]:
        return False
    return True


def _match(provider: str | None, model: str | None) -> tuple[str, str] | None:
    if not provider or not model:
        return None
    try:
        from voice_prices.data_snapshot import get_snapshot

        prov, info = get_snapshot().find_provider_model(model, None, provider, None)
        return prov.id, info.id
    except Exception:
        return None


def _unit_word(meter: str) -> str:
    for suffix in ("tokens", "seconds", "minutes", "characters", "requests"):
        if meter.endswith(suffix):
            return suffix
    return "units"


def _rate_card_rows(cards: RateCards, by_pair: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark catalog rows a rate card overrides; add rows for rate-card entries the catalog does not have."""
    extra: dict[tuple[str, str], dict[str, Any]] = {}
    today = datetime.now(tz=UTC).date()
    for rate in cards.rates:
        entry = {
            "meter": rate.meter,
            "active": rate.active(today),
            "reviewed": rate.reviewed.isoformat() if rate.reviewed else None,
            "note": rate.note,
            "multiplier": float(rate.multiplier) if rate.multiplier is not None else None,
            "unit_price": float(rate.unit_price) if rate.unit_price is not None else None,
            "unit_size": float(rate.unit_size),
        }
        target = by_pair.get((rate.provider, rate.model)) or by_pair.get(
            _match(rate.provider, rate.model) or ("", "")
        )
        if target is not None:
            target.setdefault("rate_card", []).append(entry)
            continue
        if rate.model == "*":  # a provider-wide rate applies to every catalog model of that provider
            for (prov, _), row in by_pair.items():
                if prov == rate.provider:
                    row.setdefault("rate_card", []).append({**entry, "wildcard": True})
            if any(prov == rate.provider for prov, _ in by_pair):
                continue
        row = extra.setdefault(
            (rate.provider, rate.model),
            {
                "provider": rate.provider,
                "provider_name": rate.provider,
                "pricing_tier": None,
                "model": rate.model,
                "name": None,
                "kind": "other",
                "prices": [],
                "voice_classes": None,
                "conditional": False,
                "free": False,
                "deprecated": False,
                "context_window": None,
                "freshness": None,
                "source_url": None,
                "origin": "rate_card",
                "rate_card": [],
            },
        )
        row["rate_card"].append(entry)
        if rate.unit_price is not None:
            row["prices"].append(
                {
                    "meter": rate.meter,
                    "label": rate.meter.replace("_", " ").capitalize(),
                    "price": float(rate.unit_price),
                    "unit": f"per {rate.unit_size.normalize():f} {_unit_word(rate.meter)}",
                    "tiered": False,
                }
            )
    for row in extra.values():
        row["kind"] = _kind({p["meter"] for p in row["prices"]}) if row["prices"] else "other"
    return list(extra.values())
