"""Postgres dual-store smoke tests (skipped when VOICETOLL_DB_URL is not postgres)."""

from __future__ import annotations

import os
import unittest

from voicetoll_collector.pricing import Pricer, RateCards
from voicetoll_collector.schema import CaptureEvent
from voicetoll_collector.store import Store

URL = os.environ.get("VOICETOLL_DB_URL", "")


@unittest.skipUnless(URL.startswith(("postgresql://", "postgres://")), "VOICETOLL_DB_URL is not Postgres")
class PostgresStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(URL)
        self.pricer = Pricer(RateCards.load(None))

    def tearDown(self):
        self.store.close()

    def test_insert_and_summary(self):
        event = CaptureEvent.model_validate(
            {
                "event_id": "pgtest_event_0001",
                "schema": 1,
                "source": "sdk",
                "ts": 1_700_000_000.0,
                "project": "pgdemo",
                "session": "pg-call-1",
                "component": "tts",
                "provider": "elevenlabs",
                "model": "eleven_flash_v2_5",
                "units": {"characters": 188},
                "src": {"characters": "estimated"},
                "timing_ms": {"ttfb": 180},
            }
        )
        lines = self.pricer.price(event)
        self.assertTrue(lines)
        result = self.store.insert_batch([(event, lines)])
        self.assertGreaterEqual(result.inserted + result.duplicates, 1)
        summary = self.store.session_summary("pgdemo", "pg-call-1")
        self.assertIsNotNone(summary)
        self.assertGreater(summary["cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
