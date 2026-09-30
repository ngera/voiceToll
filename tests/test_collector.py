"""End-to-end collector tests: HTTP ingest, auth, scrub, idempotency, summaries, spool and replay."""

from __future__ import annotations

import gzip
import json
import time
import unittest

from helpers import drain, ns, offline_client, temp_dir
from starlette.testclient import TestClient
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.settings import Settings

KEY = {"X-Voicetoll-Key": "dev-key"}


def make_settings() -> Settings:
    d = temp_dir()
    return Settings(
        db_url=f"sqlite:///{d / 'vt.db'}",
        ingest_keys={"dev-key": "demo"},
        spool_dir=str(d / "spool"),
        spool_max_bytes=1_000_000,
        jobs_interval_seconds=86_400,
    )


def a_call(session="call-1", tenant="clinic_17", base_ts=None) -> list[dict]:
    """Events for a two-turn LiveKit call, produced by the real client adapter."""
    from voicetoll import livekit

    client = offline_client(project="ignored-by-server")
    meter = livekit.attach(
        None,
        tenant=tenant,
        call_id=session,
        feature="reception",
        client=client,
        providers={
            "stt": ("deepgram", "nova-3"),
            "llm": ("openai", "gpt-4o-mini"),
            "tts": ("elevenlabs", "eleven_flash_v2_5"),
        },
    )
    for turn in (1, 2):
        sid = f"sp{turn}"
        meter.observe(
            ns(type="eou_metrics", end_of_utterance_delay=0.4, transcription_delay=0.2, speech_id=sid)
        )
        meter.observe(ns(type="stt_metrics", audio_duration=6.4, duration=0.0))
        meter.observe(
            ns(
                type="llm_metrics",
                prompt_tokens=890,
                completion_tokens=52,
                prompt_cached_tokens=0,
                ttft=0.31,
                duration=0.8,
                speech_id=sid,
            )
        )
        meter.observe(
            ns(
                type="tts_metrics",
                characters_count=188,
                audio_duration=11.8,
                ttfb=0.18,
                duration=0.9,
                cancelled=False,
                speech_id=sid,
            )
        )
    events = drain(client)
    start = base_ts or time.time() - 120
    for i, e in enumerate(events):
        e["ts"] = start + i * 10  # spread over ~70 s so minutes are non-zero
    return events


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.pipeline = build_pipeline(self.settings)
        self.app = create_app(self.settings, self.pipeline)

    def post(self, http, events, headers=KEY, gz=True):
        body = json.dumps({"events": events}).encode()
        hdrs = dict(headers)
        if gz:
            body = gzip.compress(body)
            hdrs["Content-Encoding"] = "gzip"
        return http.post("/v1/events", content=body, headers=hdrs)

    def test_rejects_missing_or_wrong_key(self):
        with TestClient(self.app) as http:
            self.assertEqual(self.post(http, [], headers={}).status_code, 401)
            self.assertEqual(self.post(http, [], headers={"X-Voicetoll-Key": "nope"}).status_code, 401)

    def test_ingest_price_and_summarize_a_call(self):
        with TestClient(self.app) as http:
            resp = self.post(http, a_call())
            self.assertEqual(resp.status_code, 202, resp.text)
            self.assertEqual(resp.json()["accepted"], 8)
            summary = http.get("/v1/sessions/call-1", headers=KEY).json()
        # per turn: stt $0.000512 + llm $0.0001647 + tts $0.0094 = $0.0100767; two turns
        self.assertAlmostEqual(summary["cost_usd"], 0.0201534, places=7)
        self.assertEqual(summary["turns"], 2)
        self.assertEqual(summary["components"]["tts"]["units"]["characters"], 376)
        self.assertEqual(summary["latency_ms"]["tts.ttfb"]["p50"], 180.0)
        self.assertEqual(summary["latency_ms"]["turn.eou_delay"]["n"], 2)
        self.assertTrue(summary["tenant_id"].startswith("h:"))
        self.assertIsNotNone(summary["cost_per_minute"])

    def test_project_comes_from_key_not_payload(self):
        with TestClient(self.app) as http:
            self.post(http, a_call())
            row = self.pipeline.store._query("SELECT DISTINCT project_id FROM usage_event")
        self.assertEqual(row, [{"project_id": "demo"}])

    def test_duplicates_are_ignored(self):
        events = a_call()
        with TestClient(self.app) as http:
            self.post(http, events)
            second = self.post(http, events).json()
            summary = http.get("/v1/sessions/call-1", headers=KEY).json()
        self.assertEqual(second["duplicates"], 8)
        self.assertEqual(summary["events"], 8)

    def test_scrub_drops_unknown_fields_and_rejects_invalid(self):
        good = a_call()[1]
        leaky = {
            **good,
            "event_id": "leaky-000000001",
            "text": "my card is 4111...",
            "url": "https://api.x.com/tts?text=hello",
            "tags": {"feature": "f", "phone": "+15550100"},
        }
        bad = {"event_id": "x", "component": "nope"}
        with TestClient(self.app) as http:
            result = self.post(http, [leaky, bad]).json()
        self.assertEqual((result["accepted"], result["rejected"]), (1, 1))
        rows = self.pipeline.store._query("SELECT * FROM usage_event WHERE event_id = 'leaky-000000001'")
        self.assertNotIn("4111", json.dumps(rows))
        self.assertNotIn("phone", json.dumps(rows))

    def test_tenant_daily_and_breakdowns(self):
        with TestClient(self.app) as http:
            events = a_call()
            self.post(http, events)
            tenant = events[0]["tenant"]
            daily = http.get(f"/v1/tenants/{tenant}/daily?days=2", headers=KEY).json()
            day = daily["days"][-1]["day"]
            by_feature = http.get(f"/v1/breakdown/feature?day={day}", headers=KEY).json()
            top = http.get(f"/v1/tenants?day={day}", headers=KEY).json()
        self.assertEqual(daily["days"][-1]["calls"], 1)
        self.assertEqual(by_feature["rows"][0]["key"], "reception")
        self.assertEqual(top["tenants"][0]["tenant_id"], tenant)

    def test_store_outage_spools_then_replays(self):
        events = a_call(session="call-spool")
        original = self.pipeline.store.insert_batch

        def broken(items):
            raise RuntimeError("database is down")

        self.pipeline.store.insert_batch = broken  # type: ignore[method-assign]
        with TestClient(self.app) as http:
            resp = self.post(http, events).json()
            self.assertTrue(resp["spooled"])
            self.assertEqual(len(self.pipeline.spool.pending()), 1)
            self.pipeline.store.insert_batch = original  # type: ignore[method-assign]
            self.assertEqual(self.pipeline.replay_spool(), 8)
            summary = http.get("/v1/sessions/call-spool", headers=KEY).json()
            health = http.get("/healthz").json()
        self.assertEqual(summary["events"], 8)
        self.assertEqual(health["spool_files"], 0)

    def test_spool_full_returns_503(self):
        self.pipeline.spool.max_bytes = 10

        def broken(items):
            raise RuntimeError("database is down")

        self.pipeline.store.insert_batch = broken  # type: ignore[method-assign]
        with TestClient(self.app) as http:
            self.assertEqual(self.post(http, a_call()).status_code, 503)

    def test_metrics_endpoint(self):
        with TestClient(self.app) as http:
            self.post(http, a_call())
            text = http.get("/metrics").text
        self.assertIn("voicetoll_events_stored_total 8", text)


if __name__ == "__main__":
    unittest.main()
