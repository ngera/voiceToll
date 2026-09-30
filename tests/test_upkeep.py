"""New highlight rules (release change, slow component, stale price), price freshness and automatic repricing."""

from __future__ import annotations

import time
import unittest
import uuid
from dataclasses import replace

from helpers import drain, ns, offline_client, temp_dir
from starlette.testclient import TestClient
from test_collector import KEY, make_settings
from voicetoll import livekit
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.highlights import HighlightRules, compute_highlights, turn_latencies
from voicetoll_collector.upkeep import (
    LAST_REPRICE_KEY,
    RATE_CARD_ERROR_KEY,
    apply_pricing_changes,
    check_price_freshness,
)


def _call(session: str, start: float, *, version: str = "v1", turns: int = 3, eou: float = 400.0,
          ttft: float = 300.0, ttfb: float = 150.0, chars: int = 188) -> list[dict]:
    """One call as raw capture events: per turn a turn-timing event, STT, LLM and TTS."""
    tags = {"agent_version": version, "feature": "reception"}
    events, ts = [], start

    def ev(component: str, **fields) -> dict:
        nonlocal ts
        ts += 5
        return {"event_id": uuid.uuid4().hex, "ts": ts, "session": session, "component": component,
                "tenant": "h:tenant", "tags": tags, **fields}

    for t in range(1, turns + 1):
        events.append(ev("turn", turn=t, timing_ms={"eou_delay": eou, "transcription_delay": 200}))
        events.append(ev("stt", turn=t, provider="deepgram", model="nova-3", units={"audio_input_seconds": 5.0}))
        events.append(ev("llm", turn=t, provider="openai", model="gpt-4o-mini",
                         units={"input_tokens": 900, "output_tokens": 50}, timing_ms={"ttft": ttft}))
        events.append(ev("tts", turn=t, provider="elevenlabs", model="eleven_flash_v2_5",
                         units={"characters": chars}, timing_ms={"ttfb": ttfb}))
    return events


class Base(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.pipeline = build_pipeline(self.settings)
        self.store = self.pipeline.store
        self.client = TestClient(create_app(self.settings, self.pipeline))

    def ingest(self, events):
        r = self.client.post("/v1/events", json={"events": events}, headers=KEY)
        self.assertEqual(r.status_code, 202, r.text)

    def rules(self, items, rule):
        return [h for h in items if h["rule_id"] == rule]


class TurnNumberingTests(unittest.TestCase):
    def test_eou_without_speech_id_joins_the_reply_turn(self):
        client = offline_client()
        meter = livekit.attach(None, tenant="t", call_id="c", client=client,
                               providers={"llm": ("openai", "gpt-4o-mini")})
        meter.observe(ns(type="llm_metrics", prompt_tokens=5, completion_tokens=5, speech_id="greet"))  # greeting
        for sid in ("a", "b"):
            meter.observe(ns(type="eou_metrics", end_of_utterance_delay=0.5, transcription_delay=0.2))
            meter.observe(ns(type="llm_metrics", prompt_tokens=5, completion_tokens=5, speech_id=sid))
            meter.observe(ns(type="tts_metrics", characters_count=10, speech_id=sid))
        turns = [(e["component"], e["turn"]) for e in drain(client)]
        self.assertEqual(turns, [("llm", 1), ("turn", 2), ("llm", 2), ("tts", 2), ("turn", 3), ("llm", 3), ("tts", 3)])


class LatencyRuleTests(Base):
    def test_turn_latencies_pair_events_in_order(self):
        self.ingest(_call("c1", time.time() - 600, turns=2, eou=500, ttft=300, ttfb=200))
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        turns = turn_latencies(self.store, "demo", since)
        self.assertEqual([t["v2v"] for t in turns], [1000.0, 1000.0])

    def test_slow_component_names_the_biggest_part(self):
        now = time.time()
        for i in range(5):  # 5 calls x 5 turns = 25 turns, all over 1.2 s because of turn detection
            self.ingest(_call(f"slow-{i}", now - 3000 + i * 200, turns=5, eou=2500, ttft=400, ttfb=150))
        items = self.rules(compute_highlights(self.store, "demo"), "slow_component")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["evidence"]["stage"], "eou")
        self.assertEqual(items[0]["evidence"]["turns_over_budget"], 25)
        self.assertIn("Turn detection", items[0]["title"])

    def test_no_slow_component_below_budget_or_sample_size(self):
        now = time.time()
        for i in range(5):
            self.ingest(_call(f"fast-{i}", now - 3000 + i * 200, turns=5, eou=400, ttft=300, ttfb=150))
        self.assertEqual(self.rules(compute_highlights(self.store, "demo"), "slow_component"), [])
        self.ingest(_call("few", now - 100, turns=2, eou=3000))
        rules = HighlightRules(min_turns=100)
        self.assertEqual(self.rules(compute_highlights(self.store, "demo", rules=rules), "slow_component"), [])


class ReleaseChangeTests(Base):
    def test_cost_per_minute_up_after_release(self):
        now = time.time()
        for i in range(3):
            self.ingest(_call(f"old-{i}", now - 5 * 86400 + i * 600, version="v1", chars=100))
        for i in range(3):
            self.ingest(_call(f"new-{i}", now - 2 * 86400 + i * 600, version="v2", chars=400))
        items = self.rules(compute_highlights(self.store, "demo"), "release_change")
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertIn("After release v2", item["title"])
        self.assertIn("cost per minute up", item["title"])
        self.assertGreater(item["evidence"]["cost_change"], 0.15)
        self.assertGreater(item["dollars_at_stake"], 0)

    def test_no_release_change_when_similar_or_too_few_calls(self):
        now = time.time()
        for i in range(3):
            self.ingest(_call(f"a-{i}", now - 5 * 86400 + i * 600, version="v1"))
            self.ingest(_call(f"b-{i}", now - 2 * 86400 + i * 600, version="v2"))
        self.assertEqual(self.rules(compute_highlights(self.store, "demo"), "release_change"), [])
        self.ingest(_call("c-0", now - 3600, version="v3", chars=2000))  # one call is not enough
        self.assertEqual(self.rules(compute_highlights(self.store, "demo"), "release_change"), [])


class FreshnessTests(Base):
    def test_freshness_rows_and_stale_highlight(self):
        self.ingest(_call("f1", time.time() - 600))
        rows = check_price_freshness(self.store, self.pipeline.pricer, "demo")
        by_model = {(r["provider"], r["model"]): r for r in rows}
        self.assertIn(("elevenlabs", "eleven_flash_v2_5"), by_model)
        self.assertIn(by_model[("openai", "gpt-4o-mini")]["status"], {"verified", "stale", "imported", "seed"})

        stale = {"status": "stale", "confidence": "medium", "last_verified": "2026-01-01", "age_days": 200,
                 "threshold_days": 60}
        self.pipeline.pricer.list_price_freshness = lambda *_a: stale
        check_price_freshness(self.store, self.pipeline.pricer, "demo")
        items = self.rules(compute_highlights(self.store, "demo"), "stale_price")
        self.assertEqual(len(items), 3)
        self.assertTrue(all(i["dollars_at_stake"] > 0 for i in items))
        prices = self.client.get("/v1/prices", headers=KEY).json()
        self.assertEqual({r["status"] for r in prices["rows"]}, {"stale"})

    def test_rate_card_spend_is_not_flagged(self):
        path = temp_dir() / "rates.yaml"
        path.write_text("version: t\nrates:\n  - {provider: elevenlabs, model: '*', meter: characters, "
                        "unit_price: 0.05, unit_size: 1000}\n")
        settings = replace(make_settings(), rate_cards_path=str(path))
        pipeline = build_pipeline(settings)
        pipeline.ingest(_call("r1", time.time() - 600), "demo")
        pipeline.pricer.list_price_freshness = lambda *_a: {"status": "stale", "confidence": "medium",
                                                           "last_verified": "2026-01-01", "age_days": 200,
                                                           "threshold_days": 60}
        rows = {r["provider"]: r for r in check_price_freshness(pipeline.store, pipeline.pricer, "demo")}
        self.assertEqual(rows["elevenlabs"]["status"], "rate_card")
        stale = [h["evidence"]["provider"] for h in compute_highlights(pipeline.store, "demo")
                 if h["rule_id"] == "stale_price"]
        self.assertNotIn("elevenlabs", stale)


class AutoRepriceTests(unittest.TestCase):
    def setUp(self):
        self.path = temp_dir() / "rates.yaml"
        self.path.write_text("version: one\nrates: []\n")
        self.settings = replace(make_settings(), rate_cards_path=str(self.path))
        self.pipeline = build_pipeline(self.settings)
        self.pipeline.ingest(_call("p1", time.time() - 600), "demo")
        self.store = self.pipeline.store

    def tts_cost(self):
        return self.store.session_summary("demo", "p1")["components"]["tts"]["cost_usd"]

    def test_first_run_records_baseline_then_a_rate_card_change_reprices(self):
        self.assertIsNone(apply_pricing_changes(self.pipeline, self.settings))  # baseline only
        before = self.tts_cost()
        self.path.write_text("version: two\nrates:\n  - {provider: elevenlabs, model: '*', meter: characters, "
                             "unit_price: 1.0, unit_size: 1000}\n")
        summary = apply_pricing_changes(self.pipeline, self.settings)
        self.assertEqual(summary["reason"], ["rate_card_version"])
        result = summary["projects"]["demo"]
        self.assertEqual(result["events_changed"], 3)  # only the three TTS events change
        self.assertGreater(result["delta_usd"], 0)
        self.assertAlmostEqual(self.tts_cost(), 3 * 188 * 1.0 / 1000, places=6)
        self.assertNotEqual(self.tts_cost(), before)
        self.assertEqual(self.store.get_state(LAST_REPRICE_KEY)["reason"], ["rate_card_version"])
        self.assertIsNone(apply_pricing_changes(self.pipeline, self.settings))  # nothing changed since

    def test_invalid_rate_card_keeps_previous_rates(self):
        apply_pricing_changes(self.pipeline, self.settings)
        version = self.pipeline.pricer.rate_cards.version
        self.path.write_text("rates: [ {provider: elevenlabs")  # half-saved YAML
        self.assertIsNone(apply_pricing_changes(self.pipeline, self.settings))
        self.assertEqual(self.pipeline.pricer.rate_cards.version, version)
        self.assertTrue(self.store.get_state(RATE_CARD_ERROR_KEY))

    def test_auto_reprice_can_be_turned_off(self):
        settings = replace(self.settings, auto_reprice_days=0)
        apply_pricing_changes(self.pipeline, settings)
        self.path.write_text("version: three\nrates:\n  - {provider: elevenlabs, model: '*', meter: characters, "
                             "unit_price: 1.0, unit_size: 1000}\n")
        before = self.tts_cost()
        self.assertIsNone(apply_pricing_changes(self.pipeline, settings))
        self.assertEqual(self.tts_cost(), before)  # history untouched; new events use the new rate


if __name__ == "__main__":
    unittest.main()
