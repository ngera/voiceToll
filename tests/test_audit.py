"""Per-call audit (audit.py): offline, with the provider HTTP calls replaced by canned responses."""

from __future__ import annotations

import os
import time
import unittest
from dataclasses import replace
from unittest import mock

from starlette.testclient import TestClient
from test_collector import make_settings
from voicetoll_collector import audit, reconcile
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.audit import ProviderUsage, audit_call, format_audit
from voicetoll_collector.reconcile import ReconAccount

T0 = time.time() - 600


def _ev(n: int, component: str, provider: str, model: str, units: dict, ts: float) -> dict:
    return {
        "event_id": f"evt-audit-{n:06d}",
        "ts": ts,
        "session": "audit-1",
        "component": component,
        "provider": provider,
        "model": model,
        "units": units,
    }


EVENTS = [
    _ev(1, "stt", "deepgram", "nova-3", {"audio_input_seconds": 30.0}, T0),
    _ev(2, "tts", "elevenlabs", "eleven_flash_v2_5", {"characters": 500}, T0 + 5),
    _ev(3, "llm", "openai", "gpt-4o-mini", {"input_tokens": 20, "output_tokens": 8}, T0 + 10),
]

ACCOUNTS = [
    ReconAccount(
        project="demo", provider="deepgram", key_env="VT_TEST_DG", options={"deepgram_project_id": "p1"}
    ),
    ReconAccount(project="demo", provider="elevenlabs", key_env="VT_TEST_EL"),
    ReconAccount(
        project="demo",
        provider="openai",
        key_env="VT_TEST_OA",
        dedicated=False,
        options={"openai_project_id": "proj_x"},
    ),
]


def canned(url: str, headers=None):
    """Provider responses shaped like the documented APIs."""
    if "api.elevenlabs.io/v1/history" in url:
        return {
            "history": [
                {
                    "history_item_id": "h1",
                    "date_unix": int(T0 + 5),
                    "model_id": "eleven_flash_v2_5",
                    "text": "x" * 500,
                    "character_count_change_from": 1000,
                    "character_count_change_to": 1250,
                    "request_id": "r1",
                }
            ],
            "has_more": False,
            "last_history_item_id": "h1",
        }
    if "api.deepgram.com" in url:
        return {
            "requests": [
                {
                    "request_id": "d1",
                    "created": audit._iso(T0 + 1),
                    "response": {"details": {"usd": 0.00215, "duration": 30.0, "method": "sync"}},
                }
            ]
        }
    if "api.openai.com" in url:
        return {
            "data": [
                {
                    "start_time": int(T0 // 60 * 60),
                    "results": [
                        {
                            "project_id": "proj_x",
                            "model": "gpt-4o-mini",
                            "input_tokens": 20,
                            "output_tokens": 8,
                            "input_cached_tokens": 0,
                            "num_model_requests": 1,
                        },
                        {
                            "project_id": "proj_other",
                            "model": "gpt-4o-mini",
                            "input_tokens": 999,
                            "output_tokens": 999,
                        },
                    ],
                }
            ],
            "has_more": False,
        }
    raise AssertionError(url)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = build_pipeline(make_settings())
        self.pipeline.ingest([dict(e) for e in EVENTS], "demo")
        self.env = mock.patch.dict(os.environ, {"VT_TEST_DG": "k", "VT_TEST_EL": "k", "VT_TEST_OA": "k"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_audit(self, **kw):
        with mock.patch.object(reconcile, "_get_json", side_effect=canned) as get:
            result = audit_call(self.pipeline.store, "demo", "audit-1", accounts=ACCOUNTS, **kw)
        return result, get

    def test_matching_call_is_ok_for_every_provider(self):
        result, _ = self.run_audit()
        status = {p["provider"]: p["status"] for p in result["providers"]}
        self.assertEqual(status, {"deepgram": "ok", "elevenlabs": "ok", "openai": "ok"})
        dg = next(p for p in result["providers"] if p["provider"] == "deepgram")
        self.assertIn("usd", dg["comparison"])  # Deepgram reports dollars per request
        self.assertEqual(dg["comparison"]["audio_input_seconds"]["drift"], 0.0)

    def test_window_covers_the_call_plus_padding(self):
        result, get = self.run_audit(pad_before=120, pad_after=60)
        self.assertEqual(result["window"]["start"], audit._iso(T0 - 120))
        self.assertEqual(result["window"]["end"], audit._iso(T0 + 10 + 60))
        el_url = next(c.args[0] for c in get.call_args_list if "elevenlabs" in c.args[0])
        self.assertIn(f"date_after_unix={int(T0 - 120)}", el_url)

    def test_elevenlabs_text_is_measured_not_returned_and_quota_gap_is_noted(self):
        result, _ = self.run_audit()
        el = next(p for p in result["providers"] if p["provider"] == "elevenlabs")
        self.assertEqual(el["reported"]["units"]["characters"], 500)
        self.assertEqual(el["reported"]["units"]["quota_units"], 250)
        self.assertTrue(any("quota" in n for n in el["notes"]))
        self.assertNotIn("xxxx", repr(result))  # the text itself never leaves the reader

    def test_openai_other_projects_are_excluded_and_shared_account_is_flagged(self):
        result, _ = self.run_audit()
        oa = next(p for p in result["providers"] if p["provider"] == "openai")
        self.assertEqual(oa["comparison"]["input_tokens"]["provider"], 20)
        self.assertTrue(any("shared account" in n for n in oa["notes"]))

    def test_deepgram_is_asked_for_whole_days_and_filtered_locally(self):
        _, get = self.run_audit()
        dg_url = next(c.args[0] for c in get.call_args_list if "deepgram" in c.args[0])
        self.assertNotIn("Z&", dg_url)  # Deepgram answers 400 to a trailing Z; whole days are asked for
        self.assertNotIn("endpoint=", dg_url)

    def test_deepgram_requests_outside_the_window_are_excluded_and_explained(self):
        def far(url, headers=None):
            if "api.deepgram.com" in url:
                return {
                    "requests": [
                        {
                            "request_id": "d9",
                            "created": audit._iso(T0 + 3600),
                            "path": "/v1/listen",
                            "response": {"details": {"usd": 0.01, "duration": 99.0}},
                        }
                    ]
                }
            return canned(url)

        with mock.patch.object(reconcile, "_get_json", side_effect=far):
            result = audit_call(self.pipeline.store, "demo", "audit-1", accounts=ACCOUNTS)
        dg = next(p for p in result["providers"] if p["provider"] == "deepgram")
        self.assertEqual(dg["status"], "no_provider_data")
        self.assertTrue(any("returned 1 request" in n and "min outside" in n for n in dg["notes"]))

    def test_gap_is_reported_as_drift(self):
        def short(start, end, key, options):
            return ProviderUsage(units={"audio_input_seconds": 40.0}, usd=0.003, items=[{"ref": "d1"}])

        result = audit_call(
            self.pipeline.store,
            "demo",
            "audit-1",
            accounts=ACCOUNTS,
            readers={**audit.READERS, "deepgram": short},
        )
        dg = next(p for p in result["providers"] if p["provider"] == "deepgram")
        self.assertEqual(dg["status"], "drift")
        self.assertAlmostEqual(dg["comparison"]["audio_input_seconds"]["drift"], 0.25)
        self.assertIn("DRIFT", format_audit(result))

    def test_provider_failure_and_missing_key_do_not_stop_the_others(self):
        def boom(*a):
            raise OSError("timeout")

        with mock.patch.dict(os.environ, {"VT_TEST_EL": ""}):
            result = audit_call(
                self.pipeline.store,
                "demo",
                "audit-1",
                accounts=ACCOUNTS,
                readers={**audit.READERS, "deepgram": boom, "openai": lambda *a: ProviderUsage()},
            )
        status = {p["provider"]: p["status"] for p in result["providers"]}
        self.assertEqual(
            status, {"deepgram": "fetch_failed", "elevenlabs": "skipped_no_key", "openai": "no_provider_data"}
        )

    def test_unknown_session_returns_none(self):
        self.assertIsNone(audit_call(self.pipeline.store, "demo", "nope", accounts=ACCOUNTS))

    def test_api_endpoint(self):
        settings = replace(make_settings(), admin_key="admin-secret")
        pipeline = build_pipeline(settings)
        pipeline.ingest([dict(e) for e in EVENTS], "demo")
        client = TestClient(create_app(settings, pipeline))
        self.assertEqual(client.get("/v1/audit/audit-1").status_code, 401)
        with (
            mock.patch.object(audit, "load_accounts", return_value=ACCOUNTS),
            mock.patch.object(reconcile, "_get_json", side_effect=canned),
        ):
            body = client.get("/v1/audit/audit-1", headers={"X-Voicetoll-Key": "dev-key"}).json()
            self.assertEqual({p["status"] for p in body["providers"]}, {"ok"})
            self.assertEqual(
                client.get("/v1/audit/missing", headers={"X-Voicetoll-Key": "dev-key"}).status_code, 404
            )


class ClientArrivedTests(unittest.TestCase):
    def setUp(self):
        self.store = build_pipeline(make_settings()).store
        self.started = time.time() - 60

    def save(self, sent, n, started=None):
        self.store.save_client_stats(
            "demo",
            {"client_id": "abc123def456", "sent": sent, "started_epoch": started or self.started},
            time.time(),
            batch_events=n,
        )
        return self.store.client_stats_rows(0)[0]

    def test_sent_matches_arrived_when_nothing_is_lost(self):
        row = self.save(0, 10)
        self.assertEqual((row["received_before"], row["received"]), (0, 10))
        row = self.save(10, 5)
        self.assertEqual((row["sent"], row["received_before"], row["received"]), (10, 10, 15))

    def test_counting_restarts_with_a_new_process(self):
        self.save(0, 10)
        row = self.save(0, 3, started=self.started + 100)
        self.assertEqual((row["received_before"], row["received"]), (0, 3))

    def test_first_batch_seen_mid_process_starts_from_the_clients_figure(self):
        row = self.save(500, 10)  # e.g. the collector was upgraded while the app kept running
        self.assertEqual((row["received_before"], row["received"]), (500, 510))

    def test_lost_batch_shows_as_sent_above_arrived(self):
        self.save(0, 10)
        row = self.save(20, 10)  # the client saw 20 acknowledged; only 10 were counted here
        self.assertGreater(row["sent"], row["received_before"])


if __name__ == "__main__":
    unittest.main()
