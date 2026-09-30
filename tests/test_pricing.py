"""Pricing correctness. The first three cases are the G1 gate: match a hand calculation exactly."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from decimal import Decimal

from helpers import temp_dir
from voicetoll_collector.pricing import Pricer, RateCards
from voicetoll_collector.schema import CaptureEvent

WHEN = datetime(2026, 9, 27, 14, 0, tzinfo=UTC)


def ev(provider, model, units, component="tts", **kw):
    return CaptureEvent(
        event_id="evt-00000001",
        ts=WHEN,
        session="s",
        component=component,
        provider=provider,
        model=model,
        units=units,
        **kw,
    )


def total(lines):
    return sum((line.amount_usd for line in lines), Decimal(0))


class ListPriceTests(unittest.TestCase):
    def setUp(self):
        self.pricer = Pricer()

    def test_tts_characters_hand_calc(self):
        # 188 characters x $0.05 / 1,000 characters = $0.0094
        lines = self.pricer.price(ev("elevenlabs", "eleven_flash_v2_5", {"characters": 188}))
        self.assertEqual(total(lines), Decimal("0.0094"))
        self.assertEqual(lines[0].price_source, "voice_prices")
        self.assertIsNotNone(lines[0].freshness)

    def test_stt_seconds_hand_calc(self):
        # 6.4 s x $0.08 / 1,000 s ($0.0048/min) = $0.000512
        lines = self.pricer.price(ev("deepgram", "nova-3", {"audio_input_seconds": 6.4}, component="stt"))
        self.assertEqual(total(lines), Decimal("0.000512"))

    def test_llm_tokens_hand_calc_and_split_per_meter(self):
        # 890 x $0.15/M + 52 x $0.60/M = $0.0001335 + $0.0000312
        lines = self.pricer.price(
            ev("openai", "gpt-4o-mini", {"input_tokens": 890, "output_tokens": 52}, component="llm")
        )
        by_meter = {line.meter: line.amount_usd for line in lines}
        self.assertEqual(by_meter["input_tokens"], Decimal("0.0001335"))
        self.assertEqual(by_meter["output_tokens"], Decimal("0.0000312"))

    def test_unknown_model_is_unpriced_not_free(self):
        lines = self.pricer.price(ev("elevenlabs", "does-not-exist", {"characters": 100}))
        self.assertEqual(lines[0].price_source, "unpriced")
        self.assertEqual(lines[0].unpriced_reason, "model_unknown")

    def test_unit_the_model_does_not_bill_is_flagged(self):
        # OpenAI TTS bills tokens; seconds cannot be priced for it
        lines = self.pricer.price(ev("openai", "gpt-4o-mini-tts", {"audio_input_seconds": 6}))
        self.assertEqual(lines[0].price_source, "unpriced")
        self.assertEqual(lines[0].unpriced_reason, "no_rate_for_unit")

    def test_informational_unit_is_not_billed_rather_than_unpriced(self):
        # ElevenLabs bills characters; the audio seconds LiveKit also reports carry no price
        lines = self.pricer.price(
            ev("elevenlabs", "eleven_flash_v2_5", {"characters": 188, "audio_output_seconds": 11.8})
        )
        sources = {line.meter: line.price_source for line in lines}
        self.assertEqual(sources, {"characters": "voice_prices", "audio_output_seconds": "not_billed"})
        self.assertEqual(total(lines), Decimal("0.0094"))

    def test_turn_events_without_units_have_no_lines(self):
        self.assertEqual(self.pricer.price(ev(None, None, {}, component="turn")), [])


class RateCardTests(unittest.TestCase):
    def setUp(self):
        path = temp_dir() / "rates.yaml"
        path.write_text(
            "version: test\n"
            "rates:\n"
            "  - {provider: elevenlabs, model: eleven_flash_v2_5, meter: characters, unit_price: 0.04,"
            " unit_size: 1000, effective_from: '2026-09-01'}\n"
            "  - {provider: deepgram, model: '*', meter: audio_input_seconds, multiplier: 0.8}\n"
            "  - {provider: speechify, model: '*', meter: characters, unit_price: 0.01, unit_size: 1000}\n"
        )
        self.pricer = Pricer(RateCards.load(str(path)))

    def test_absolute_rate_overrides_list_price(self):
        lines = self.pricer.price(ev("elevenlabs", "eleven_flash_v2_5", {"characters": 1000}))
        self.assertEqual(total(lines), Decimal("0.04"))
        self.assertEqual(lines[0].price_source, "rate_card")
        self.assertTrue(lines[0].rate_card_version.startswith("test+"))

    def test_rate_not_yet_effective_falls_back_to_list(self):
        early = CaptureEvent(
            event_id="evt-00000002",
            ts=datetime(2026, 8, 1, tzinfo=UTC),
            session="s",
            component="tts",
            provider="elevenlabs",
            model="eleven_flash_v2_5",
            units={"characters": 1000},
        )
        self.assertEqual(self.pricer.price(early)[0].price_source, "voice_prices")

    def test_multiplier_discounts_list_price(self):
        lines = self.pricer.price(ev("deepgram", "nova-3", {"audio_input_seconds": 6.4}, component="stt"))
        self.assertEqual(total(lines), Decimal("0.000512") * Decimal("0.8"))

    def test_rate_card_prices_a_provider_missing_from_the_catalog(self):
        lines = self.pricer.price(ev("speechify", "simba", {"characters": 2000}))
        self.assertEqual(total(lines), Decimal("0.02"))


if __name__ == "__main__":
    unittest.main()
