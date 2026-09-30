"""All-prices catalog (catalog.py, GET /v1/admin/catalog)."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from starlette.testclient import TestClient
from test_collector import make_settings
from voicetoll_collector.app import build_pipeline, create_app

ADMIN = {"X-Voicetoll-Admin-Key": "admin-secret"}

RATE_CARD = """
version: test
rates:
  - provider: elevenlabs
    model: eleven_flash_v2_5
    meter: characters
    unit_price: 0.03
    unit_size: 1000
  - provider: acme-voice
    model: house-tts
    meter: characters
    unit_price: 0.02
    unit_size: 1000
"""


def _event(n: int, provider: str, model: str, units: dict) -> dict:
    return {
        "event_id": f"evt-cat-{n:07d}",
        "ts": time.time() - 60,
        "session": "c1",
        "component": "tts",
        "provider": provider,
        "model": model,
        "units": units,
    }


class CatalogTests(unittest.TestCase):
    def setUp(self):
        card = Path(tempfile.mkdtemp()) / "rates.yaml"
        card.write_text(RATE_CARD)
        self.settings = replace(make_settings(), admin_key="admin-secret", rate_cards_path=str(card))
        pipeline = build_pipeline(self.settings)
        pipeline.ingest(
            [
                _event(1, "openai", "gpt-4o-mini-2024-07-18", {"input_tokens": 10}),
                _event(2, "made-up", "mystery-1", {"characters": 5}),
            ],
            "demo",
        )
        self.client = TestClient(create_app(self.settings, pipeline))

    def get(self, **params):
        res = self.client.get("/v1/admin/catalog", params=params, headers=ADMIN)
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()

    def test_needs_the_admin_key(self):
        self.assertEqual(self.client.get("/v1/admin/catalog").status_code, 401)

    def test_lists_the_whole_catalog_one_page_at_a_time(self):
        body = self.get(limit=50)
        self.assertGreater(body["total"], 100)
        self.assertEqual(len(body["rows"]), 50)
        self.assertEqual(self.get(limit=50, offset=50)["rows"][0]["model"] != body["rows"][0]["model"], True)

    def test_prices_are_in_the_units_providers_quote_with_the_meter_to_use(self):
        (row,) = [
            r
            for r in self.get(provider="openai", q="gpt-4o-mini", limit=500)["rows"]
            if r["model"] == "gpt-4o-mini"
        ]
        prices = {p["meter"]: p for p in row["prices"]}
        self.assertEqual(prices["input_tokens"]["unit"], "per 1M tokens")
        self.assertEqual(row["kind"], "llm")
        dg = [
            r for r in self.get(provider="deepgram", kind="stt", limit=500)["rows"] if r["model"] == "nova-3"
        ][0]
        self.assertEqual(dg["prices"][0]["unit"], "per minute")
        self.assertEqual(dg["prices"][0]["meter"], "audio_input_seconds")

    def test_in_use_matches_dated_aliases_and_comes_first(self):
        body = self.get(in_use="1")
        self.assertEqual(body["in_use_count"], 1)
        (row,) = body["rows"]
        self.assertEqual((row["provider"], row["model"]), ("openai", "gpt-4o-mini"))
        self.assertIn("gpt-4o-mini-2024-07-18", row["sent_as"])
        self.assertEqual(self.get(limit=1)["rows"][0]["model"], "gpt-4o-mini")

    def test_unknown_models_in_use_are_listed(self):
        unmatched = self.get()["unmatched_in_use"]
        self.assertIn({"provider": "made-up", "model": "mystery-1", "events": 1}, unmatched)

    def test_rate_cards_mark_overrides_and_add_their_own_rows(self):
        rows = self.get(rate_card="1", limit=500)["rows"]
        pairs = {(r["provider"], r["model"]): r for r in rows}
        self.assertEqual(pairs[("elevenlabs", "eleven_flash_v2_5")]["rate_card"][0]["unit_price"], 0.03)
        house = pairs[("acme-voice", "house-tts")]
        self.assertEqual((house["origin"], house["kind"]), ("rate_card", "tts"))
        self.assertEqual(house["prices"][0]["unit"], "per 1000 characters")

    def test_filters(self):
        self.assertTrue(all(r["kind"] == "tts" for r in self.get(kind="tts", limit=500)["rows"]))
        self.assertTrue(all(not r["deprecated"] for r in self.get(limit=500)["rows"]))
        self.assertTrue(all(not r["free"] for r in self.get(free="0", limit=500)["rows"]))
        self.assertEqual(self.get(q="no-such-model-anywhere")["matched"], 0)


if __name__ == "__main__":
    unittest.main()
