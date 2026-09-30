"""Turn raw units into cost lines: private rate cards first, then voice-prices list prices."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .schema import CaptureEvent

log = logging.getLogger("voicetoll.collector.pricing")

INT_UNITS = {
    "characters",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_audio_tokens",
    "output_audio_tokens",
    "cache_audio_read_tokens",
}

# Which voice-prices PriceBreakdown fields make up the cost of each unit.
UNIT_TO_BREAKDOWN: dict[str, tuple[str, ...]] = {
    "characters": ("input_kchars", "voice_class_input_adjustment"),
    "audio_input_seconds": ("input_audio_kseconds",),
    "audio_output_seconds": ("output_audio_kseconds", "voice_class_output_adjustment"),
    "input_tokens": ("input_tokens",),
    "output_tokens": ("output_tokens",),
    "cache_read_tokens": ("cache_read_tokens",),
    "cache_write_tokens": ("cache_write_tokens",),
    "input_audio_tokens": ("input_audio_tokens",),
    "output_audio_tokens": ("output_audio_tokens",),
    "cache_audio_read_tokens": ("cache_audio_read_tokens",),
    "agent_minutes": ("agent_kminutes",),
    "telephony_minutes": ("telephony_kminutes",),
}


@dataclass
class CostLine:
    meter: str
    quantity: Decimal
    unit_src: str
    unit_how: str | None
    amount_usd: Decimal
    price_source: str  # rate_card | voice_prices | not_billed | unpriced
    price_version: str
    rate_card_version: str
    freshness: str | None = None
    unpriced_reason: str | None = None


# ---- rate cards -----------------------------------------------------------------------------
@dataclass
class Rate:
    provider: str
    model: str
    meter: str
    unit_price: Decimal | None = None
    unit_size: Decimal = Decimal(1)
    multiplier: Decimal | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    note: str | None = None
    reviewed: date | None = None  # when someone last checked this rate against the contract or plan

    def active(self, day: date) -> bool:
        if self.effective_from and day < self.effective_from:
            return False
        if self.effective_to and day > self.effective_to:
            return False
        return True


def _to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


@dataclass
class RateCards:
    version: str = ""
    rates: list[Rate] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | None) -> RateCards:
        if not path or not Path(path).exists():
            return cls()
        raw_bytes = Path(path).read_bytes()
        data = yaml.safe_load(raw_bytes) or {}
        rates = []
        for item in data.get("rates", []) or []:
            if item.get("unit_price") is None and item.get("multiplier") is None:
                raise ValueError(f"rate card entry needs unit_price or multiplier: {item}")
            rates.append(
                Rate(
                    provider=str(item["provider"]).lower(),
                    model=str(item.get("model", "*")),
                    meter=str(item["meter"]),
                    unit_price=Decimal(str(item["unit_price"]))
                    if item.get("unit_price") is not None
                    else None,
                    unit_size=Decimal(str(item.get("unit_size", 1))),
                    multiplier=Decimal(str(item["multiplier"]))
                    if item.get("multiplier") is not None
                    else None,
                    effective_from=_to_date(item.get("effective_from")),
                    effective_to=_to_date(item.get("effective_to")),
                    note=item.get("note"),
                    reviewed=_to_date(item.get("reviewed")),
                )
            )
        version = f"{data.get('version', 'unversioned')}+{hashlib.sha256(raw_bytes).hexdigest()[:8]}"
        return cls(version=version, rates=rates)

    def entries_for(self, provider: str | None, model: str | None, meter: str) -> list[Rate]:
        """Every entry for this provider/model/meter (exact model first, then "*"), active or not."""
        exact = [r for r in self.rates if r.provider == provider and r.meter == meter and r.model == model]
        wild = [r for r in self.rates if r.provider == provider and r.meter == meter and r.model == "*"]
        return exact + wild

    def find(self, provider: str | None, model: str | None, meter: str, day: date) -> Rate | None:
        best: Rate | None = None
        for rate in self.rates:
            if rate.provider != provider or rate.meter != meter or not rate.active(day):
                continue
            if rate.model == model:
                return rate
            if rate.model == "*" and best is None:
                best = rate
        return best


# ---- pricer ---------------------------------------------------------------------------------
class Pricer:
    def __init__(self, rate_cards: RateCards | None = None) -> None:
        import voice_prices

        self._vp = voice_prices
        self.rate_cards = rate_cards or RateCards()
        self.price_version = f"voice-prices {getattr(voice_prices, '__version__', 'unknown')}"

    def list_price_freshness(self, provider: str, model: str, today: date) -> dict[str, Any] | None:
        """voice-prices freshness for one provider/model today, or None when the catalog has no such model."""
        try:
            from voice_prices.confidence import model_freshness
            from voice_prices.data_snapshot import get_snapshot

            prov, info = get_snapshot().find_provider_model(model, None, provider, None)
            fresh = model_freshness(info, prov, today=today)
        except (LookupError, ImportError, AttributeError, ValueError):
            return None
        return {
            "status": fresh.verification_status,
            "confidence": fresh.confidence,
            "last_verified": fresh.last_verified.isoformat() if fresh.last_verified else None,
            "age_days": fresh.age_days,
            "threshold_days": getattr(prov, "staleness_threshold_days", None),
        }

    def _list_price(self, event: CaptureEvent) -> tuple[Any | None, str | None]:
        if not event.model:
            return None, "model_missing"
        if not event.provider:
            return None, "provider_missing"
        usage_kwargs: dict[str, Any] = {}
        for unit, value in event.units.items():
            usage_kwargs[unit] = int(round(value)) if unit in INT_UNITS else Decimal(str(value))
        if event.voice_class:
            usage_kwargs["voice_class"] = event.voice_class
        try:
            calc = self._vp.calc_price(
                self._vp.Usage(**usage_kwargs),
                model_ref=event.model,
                provider_id=event.provider,
                genai_request_timestamp=event.ts,
            )
        except LookupError as exc:
            return None, "provider_unknown" if "provider" in str(exc).split("with")[0] else "model_unknown"
        except Exception as exc:  # e.g. ValueError for audio tokens larger than the total: never block ingest
            log.info("voice-prices rejected usage for %s/%s: %s", event.provider, event.model, exc)
            return None, "invalid_usage"
        return calc, None

    def price(self, event: CaptureEvent) -> list[CostLine]:
        """Cost lines for one event. Never raises: anything unexpected becomes unpriced lines with a reason,
        so a single odd event can never fail (or spool) the batch it arrived in."""
        try:
            return self._price(event)
        except Exception as exc:
            log.warning(
                "pricing failed for event %s (%s/%s): %s", event.event_id, event.provider, event.model, exc
            )
            return [
                CostLine(
                    meter=unit,
                    quantity=Decimal(str(value)),
                    unit_src=event.unit_source(unit),
                    unit_how=event.how.get(unit),
                    amount_usd=Decimal(0),
                    price_source="unpriced",
                    price_version=self.price_version,
                    rate_card_version="",
                    unpriced_reason="price_error",
                )
                for unit, value in event.units.items()
            ]

    def _price(self, event: CaptureEvent) -> list[CostLine]:
        if not event.units:
            return []
        day = event.ts.date()
        calc, reason = self._list_price(event)
        freshness = None
        if calc is not None:
            try:
                freshness = calc.freshness(today=day).verification_status
            except Exception:
                freshness = None
        unpriced_units = set(getattr(calc, "unpriced_usage", ()) or ()) if calc is not None else set()
        # A unit the model does not bill (e.g. TTS audio seconds on a per-character model) is informational
        # when another unit on the same event was priced; it is only "unpriced" when nothing could be priced.
        billed_units = set(event.units) - unpriced_units if calc is not None else set()
        lines: list[CostLine] = []
        for unit, value in event.units.items():
            quantity = Decimal(str(value))
            list_amount: Decimal | None = None
            if calc is not None and unit not in unpriced_units:
                list_amount = sum(
                    (Decimal(getattr(calc.breakdown, f, 0) or 0) for f in UNIT_TO_BREAKDOWN.get(unit, ())),
                    Decimal(0),
                )
            rate = self.rate_cards.find(event.provider, event.model, unit, day)
            common = dict(
                meter=unit,
                quantity=quantity,
                unit_src=event.unit_source(unit),
                unit_how=event.how.get(unit),
                price_version=self.price_version,
                rate_card_version="",
                freshness=freshness,
            )
            if rate is not None and rate.unit_price is not None:
                amount = quantity * rate.unit_price / rate.unit_size
                lines.append(
                    CostLine(
                        amount_usd=amount,
                        price_source="rate_card",
                        **{**common, "rate_card_version": self.rate_cards.version, "freshness": None},
                    )
                )
            elif rate is not None and rate.multiplier is not None and list_amount is not None:
                lines.append(
                    CostLine(
                        amount_usd=list_amount * rate.multiplier,
                        price_source="rate_card",
                        **{**common, "rate_card_version": self.rate_cards.version},
                    )
                )
            elif list_amount is not None:
                lines.append(CostLine(amount_usd=list_amount, price_source="voice_prices", **common))
            elif unit in unpriced_units and billed_units:
                lines.append(CostLine(amount_usd=Decimal(0), price_source="not_billed", **common))
            else:
                why = reason or ("no_rate_for_unit" if unit in unpriced_units else "no_rate")
                lines.append(
                    CostLine(amount_usd=Decimal(0), price_source="unpriced", unpriced_reason=why, **common)
                )
        # Per-request fees have no unit of their own; attach them as a separate meter.
        if calc is not None:
            requests_fee = Decimal(getattr(calc.breakdown, "requests", 0) or 0)
            if requests_fee:
                lines.append(
                    CostLine(
                        meter="requests",
                        quantity=Decimal(1),
                        unit_src="reported",
                        unit_how=None,
                        amount_usd=requests_fee,
                        price_source="voice_prices",
                        price_version=self.price_version,
                        rate_card_version="",
                        freshness=freshness,
                    )
                )
        return lines


def price_units(
    provider: str,
    model: str,
    units: dict[str, float],
    when: datetime | None = None,
    rate_cards: RateCards | None = None,
) -> list[CostLine]:
    """Convenience for scripts and the CLI: price a bare set of units."""
    from datetime import UTC

    event = CaptureEvent(
        event_id="cli-price-000",
        ts=when or datetime.now(tz=UTC),
        session="cli",
        component="tts",
        provider=provider,
        model=model,
        units=units,
    )
    return Pricer(rate_cards).price(event)
