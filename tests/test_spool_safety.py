"""A single bad event must never spool its batch or block the spool (the "poison batch" bug)."""

from __future__ import annotations

import json
import time
import unittest

from test_collector import make_settings
from voicetoll_collector.app import build_pipeline


def _event(event_id: str, **kw) -> dict:
    return {"event_id": event_id, "ts": time.time() - 60, "session": "c1", **kw}


class SpoolSafetyTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = build_pipeline(make_settings())

    def test_event_voice_prices_rejects_is_stored_unpriced_not_spooled(self):
        # Audio tokens without a total make voice-prices raise ValueError
        bad = _event(
            "evt-s2s-000001",
            component="s2s",
            provider="openai",
            model="gpt-realtime",
            units={"input_audio_tokens": 1000, "output_audio_tokens": 800},
        )
        good = _event(
            "evt-tts-000001",
            component="tts",
            provider="elevenlabs",
            model="eleven_flash_v2_5",
            units={"characters": 100},
        )
        result = self.pipeline.ingest([bad, good], "demo")
        self.assertFalse(result["spooled"])
        self.assertEqual(self.pipeline.spool.pending(), [])
        lines = self.pipeline.store.current_lines_by_event(["evt-s2s-000001", "evt-tts-000001"])
        self.assertEqual({r["price_source"] for r in lines["evt-s2s-000001"]}, {"unpriced"})
        reasons = self.pipeline.store._query(
            "SELECT DISTINCT unpriced_reason FROM cost_line WHERE event_id = ?", ("evt-s2s-000001",)
        )
        self.assertEqual(reasons[0]["unpriced_reason"], "invalid_usage")
        self.assertGreater(sum(float(r["amount_usd"]) for r in lines["evt-tts-000001"]), 0)

    def test_pricer_never_raises(self):
        class Boom:
            def calc_price(self, *a, **k):
                raise RuntimeError("catalog exploded")

            def Usage(self, **k):  # noqa: N802
                return k

        self.pipeline.pricer._vp = Boom()
        result = self.pipeline.ingest(
            [
                _event(
                    "evt-llm-000001",
                    component="llm",
                    provider="openai",
                    model="gpt-4o-mini",
                    units={"input_tokens": 10},
                )
            ],
            "demo",
        )
        self.assertFalse(result["spooled"])
        self.assertEqual(result["accepted"], 1)

    def test_invalid_spooled_events_are_dropped_not_blocking(self):
        spool = self.pipeline.spool
        spool.write(
            [
                {"event_id": "x"},
                _event(
                    "evt-ok-0000001",
                    component="tts",
                    provider="elevenlabs",
                    model="eleven_flash_v2_5",
                    units={"characters": 5},
                    project="demo",
                ),
            ]
        )
        self.assertEqual(self.pipeline.replay_spool(), 2)
        self.assertEqual(spool.pending(), [])

    def test_file_that_keeps_failing_with_storage_up_moves_to_rejected(self):
        spool = self.pipeline.spool
        spool.write([_event("evt-a-00000001", component="tts", units={"characters": 5}, project="demo")])
        spool.write([_event("evt-b-00000001", component="tts", units={"characters": 5}, project="demo")])

        def fail_first(events):
            if events[0]["event_id"] == "evt-a-00000001":
                raise RuntimeError("bad batch")

        for _ in range(2):
            self.assertEqual(spool.replay(fail_first, storage_ok=lambda: True), 0)
            self.assertEqual(len(spool.pending()), 2)
        self.assertEqual(spool.replay(fail_first, storage_ok=lambda: True), 1)  # third failure: quarantined
        self.assertEqual(spool.pending(), [])
        self.assertEqual(len(spool.rejected()), 1)
        self.assertIn(
            "evt-a-00000001", json.loads(spool.rejected()[0].read_text().splitlines()[0])["event_id"]
        )

    def test_storage_outage_never_quarantines(self):
        spool = self.pipeline.spool
        spool.write([_event("evt-c-00000001", component="tts", units={"characters": 5}, project="demo")])

        def down(events):
            raise ConnectionError("db down")

        for _ in range(5):
            spool.replay(down, storage_ok=lambda: False)
        self.assertEqual(len(spool.pending()), 1)
        self.assertEqual(spool.rejected(), [])


if __name__ == "__main__":
    unittest.main()
