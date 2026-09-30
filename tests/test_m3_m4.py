"""OTLP ingest, highlights, coverage, reprice, reconcile, Vapi import."""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from helpers import temp_dir
from starlette.testclient import TestClient
from test_collector import KEY, a_call, make_settings
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.otlp import otlp_json_to_events
from voicetoll_collector.reconcile import run_reconciliation
from voicetoll_collector.reprice import reprice
from voicetoll_collector.vapi_import import call_to_events, import_vapi_calls


class OtlpTests(unittest.TestCase):
    def test_maps_genai_span(self):
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "abc",
                                    "spanId": "span1",
                                    "name": "chat gpt-4o-mini",
                                    "startTimeUnixNano": str(int(time.time() * 1e9)),
                                    "attributes": [
                                        {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                                        {
                                            "key": "gen_ai.request.model",
                                            "value": {"stringValue": "gpt-4o-mini"},
                                        },
                                        {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                                        {"key": "voicetoll.session", "value": {"stringValue": "otlp-call-1"}},
                                        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "100"}},
                                        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "20"}},
                                        {"key": "voicetoll.timing.ttft_ms", "value": {"doubleValue": 310}},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        events = otlp_json_to_events(payload, project="demo")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["component"], "llm")
        self.assertEqual(events[0]["provider"], "openai")
        self.assertEqual(events[0]["units"]["input_tokens"], 100)
        self.assertEqual(events[0]["timing_ms"]["ttft"], 310)

    def test_http_otlp_endpoint(self):
        settings = make_settings()
        settings.jobs_interval_seconds = 86_400
        app = create_app(settings, build_pipeline(settings))
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "t1",
                                    "spanId": "s1",
                                    "name": "tts",
                                    "attributes": [
                                        {"key": "voicetoll.component", "value": {"stringValue": "tts"}},
                                        {"key": "voicetoll.session", "value": {"stringValue": "otlp-sess"}},
                                        {"key": "voicetoll.provider", "value": {"stringValue": "elevenlabs"}},
                                        {
                                            "key": "voicetoll.model",
                                            "value": {"stringValue": "eleven_flash_v2_5"},
                                        },
                                        {"key": "voicetoll.units.characters", "value": {"doubleValue": 188}},
                                    ],
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        with TestClient(app) as http:
            resp = http.post(
                "/v1/otlp/v1/traces",
                content=json.dumps(payload).encode(),
                headers={**KEY, "Content-Type": "application/json", "Accept": "application/json"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertGreaterEqual(resp.json()["voicetoll"]["accepted"], 1)
            summary = http.get("/v1/sessions/otlp-sess", headers=KEY)
            self.assertEqual(summary.status_code, 200)


class HighlightsCoverageTests(unittest.TestCase):
    def test_highlights_and_coverage(self):
        settings = make_settings()
        settings.jobs_interval_seconds = 86_400
        pipeline = build_pipeline(settings)
        app = create_app(settings, pipeline)
        with TestClient(app) as http:
            self.assertEqual(http.post("/v1/events", json={"events": a_call()}, headers=KEY).status_code, 202)
            cov = http.get("/v1/coverage", headers=KEY)
            self.assertEqual(cov.status_code, 200)
            self.assertGreater(cov.json()["events"], 0)
            hl = http.get("/v1/highlights?days=7", headers=KEY)
            self.assertEqual(hl.status_code, 200)
            self.assertIn("highlights", hl.json())


class RepriceReconcileVapiTests(unittest.TestCase):
    def test_reprice_supersedes(self):
        settings = make_settings()
        pipeline = build_pipeline(settings)
        events = a_call(session="reprice-1")
        pipeline.ingest(events, "demo")
        before = pipeline.store.session_summary("demo", "reprice-1")["cost_usd"]
        result = reprice(pipeline.store, pipeline.pricer, "demo", provider="elevenlabs")
        self.assertGreater(result["events"], 0)
        after = pipeline.store.session_summary("demo", "reprice-1")["cost_usd"]
        self.assertAlmostEqual(before, after, places=6)

    def test_reconcile_with_stub_fetcher(self):
        settings = make_settings()
        pipeline = build_pipeline(settings)
        pipeline.ingest(a_call(session="recon-1"), "demo")
        day = time.strftime("%Y-%m-%d", time.gmtime())

        def stub(_day: str, _key: str) -> float:
            return pipeline.store.estimated_provider_day("demo", "elevenlabs", day)

        rows = run_reconciliation(
            pipeline.store,
            "demo",
            day,
            fetchers={"elevenlabs": stub},
            api_keys={"elevenlabs": "x"},
        )
        self.assertEqual(rows[0]["status"], "ok")
        self.assertLessEqual(rows[0]["drift_pct"], 0.05)

    def test_vapi_import(self):
        call = {
            "id": "vapi-call-9",
            "orgId": "org_1",
            "startedAt": "2026-09-27T12:00:00.000Z",
            "usage": {
                "stt": {"provider": "deepgram", "model": "nova-3", "seconds": 12.0},
                "llm": {
                    "provider": "openai",
                    "model": "gpt-4o-mini",
                    "promptTokens": 400,
                    "completionTokens": 40,
                },
                "tts": {"provider": "elevenlabs", "model": "eleven_flash_v2_5", "characters": 200},
            },
        }
        events = call_to_events(call, project="demo")
        self.assertEqual({e["component"] for e in events}, {"stt", "llm", "tts"})
        d = temp_dir()
        path = Path(d) / "call.json"
        path.write_text(json.dumps(call), encoding="utf-8")
        settings = make_settings()
        pipeline = build_pipeline(settings)
        imported = import_vapi_calls(path, project="demo")
        result = pipeline.ingest(imported, "demo")
        self.assertEqual(result["accepted"], 3)


if __name__ == "__main__":
    unittest.main()
