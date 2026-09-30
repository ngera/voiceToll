"""Reconciliation: dollars and units, dedicated vs shared accounts, consecutive-day alerts, connectors."""

from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

from helpers import temp_dir
from test_collector import a_call, make_settings
from voicetoll_collector import reconcile as rc
from voicetoll_collector.app import build_pipeline
from voicetoll_collector.highlights import compute_highlights
from voicetoll_collector.reconcile import Report, ReconAccount, load_accounts, reconcile_account


def _day(offset: int) -> tuple[str, float]:
    """(YYYY-MM-DD, epoch at 12:00 UTC) for today minus `offset` days."""
    d = datetime.now(tz=UTC).replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=offset)
    return d.strftime("%Y-%m-%d"), d.timestamp()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = build_pipeline(make_settings())
        self.store = self.pipeline.store
        self.days = []
        for offset in (2, 1):
            day, ts = _day(offset)
            self.pipeline.ingest(a_call(session=f"recon-{offset}", base_ts=ts), "demo")
            self.days.append(day)
        self.eleven = ReconAccount(project="demo", provider="elevenlabs", key_env="")

    def run_with(self, account, day, report, **kw):
        return reconcile_account(self.store, account, day, fetcher=lambda *_a: report, api_key="k", **kw)

    def test_unit_drift_for_a_provider_without_dollars(self):
        day = self.days[0]
        est = self.store.estimated_provider_units("demo", "elevenlabs", day)["characters"]
        row = self.run_with(self.eleven, day, Report(units={"characters": est * 1.25}))
        self.assertEqual(row["status"], "drift")
        self.assertAlmostEqual(row["detail"]["unit_drift"]["characters"], 0.2, places=3)
        self.assertIsNone(row["reported_usd"])

    def test_matching_units_are_ok(self):
        day = self.days[0]
        est = self.store.estimated_provider_units("demo", "elevenlabs", day)["characters"]
        row = self.run_with(self.eleven, day, Report(units={"characters": est * 1.02}))
        self.assertEqual(row["status"], "ok")

    def test_two_days_of_drift_raise_an_alert_and_a_highlight(self):
        for day in self.days:
            est = self.store.estimated_provider_units("demo", "elevenlabs", day)["characters"]
            row = self.run_with(self.eleven, day, Report(units={"characters": est * 2}))
        self.assertEqual(row["detail"]["consecutive_drift_days"], 2)
        self.assertTrue(row["detail"]["alert"])
        rules = {h["rule_id"] for h in compute_highlights(self.store, "demo", days=7)}
        self.assertIn("recon_drift", rules)

    def test_shared_account_drift_never_alerts(self):
        shared = ReconAccount(project="demo", provider="elevenlabs", key_env="", dedicated=False)
        for day in self.days:
            row = self.run_with(shared, day, Report(units={"characters": 1_000_000}))
        self.assertEqual(row["status"], "drift_shared_account")
        self.assertFalse(row["detail"]["alert"])

    def test_rerun_same_day_overwrites(self):
        day = self.days[0]
        self.run_with(self.eleven, day, Report(units={"characters": 1}))
        row = self.run_with(self.eleven, day, Report(units={"characters": 1}))
        self.assertEqual(len(self.store.recon_runs_since("demo", day)), 1)  # upserted, not duplicated
        self.assertEqual(row["status"], "drift")

    def test_missing_key_and_failed_fetch(self):
        acct = ReconAccount(project="demo", provider="openai", key_env="VOICETOLL_TEST_UNSET_KEY")
        self.assertEqual(reconcile_account(self.store, acct, self.days[0])["status"], "skipped_no_key")
        failed = reconcile_account(self.store, acct, self.days[0], fetcher=lambda *_a: None, api_key="k")
        self.assertEqual(failed["status"], "fetch_failed")

    def test_legacy_two_argument_float_fetcher_still_works(self):
        day = self.days[0]
        est = self.store.estimated_provider_day("demo", "openai", day)
        acct = ReconAccount(project="demo", provider="openai", key_env="")
        row = reconcile_account(self.store, acct, day, fetcher=lambda _d, _k: est, api_key="k")
        self.assertEqual(row["status"], "ok")


class ConfigTests(unittest.TestCase):
    def test_load_accounts(self):
        path = temp_dir() / "reconcile.yaml"
        path.write_text(
            "accounts:\n"
            "  - {project: demo, provider: OpenAI, key_env: K1, options: {openai_project_id: proj_1}}\n"
            "  - {project: demo, provider: deepgram, key_env: K2, scope: shared}\n"
        )
        accounts = load_accounts(str(path))
        self.assertEqual([(a.provider, a.dedicated) for a in accounts], [("openai", True), ("deepgram", False)])
        self.assertEqual(accounts[0].options["openai_project_id"], "proj_1")
        self.assertEqual(load_accounts(str(path.parent / "missing.yaml")), [])

    def test_bad_scope_rejected(self):
        path = temp_dir() / "reconcile.yaml"
        path.write_text("accounts:\n  - {project: demo, provider: openai, key_env: K, scope: team}\n")
        with self.assertRaises(ValueError):
            load_accounts(str(path))


class ConnectorParsingTests(unittest.TestCase):
    """Connector parsing against sample payloads (no network). Shapes to be pinned with real keys."""

    def test_openai_sums_costs_and_filters_by_project(self):
        seen = {}

        def fake(url, headers=None):
            seen["url"] = url
            return {"data": [{"results": [{"amount": {"value": 1.25}}, {"amount": {"value": 0.5}}]}]}

        with mock.patch.object(rc, "_get_json", fake):
            report = rc.fetch_openai("2026-09-27", "k", {"openai_project_id": "proj_9"})
        self.assertAlmostEqual(report.usd, 1.75)
        self.assertIn("project_ids=proj_9", seen["url"])

    def test_elevenlabs_sums_characters(self):
        with mock.patch.object(rc, "_get_json", lambda *_a, **_k: {"time": [1, 2], "usage": {"All": [1000, 500]}}):
            report = rc.fetch_elevenlabs("2026-09-27", "k")
        self.assertEqual(report.units, {"characters": 1500.0})
        self.assertIsNone(report.usd)

    def test_deepgram_hours_become_seconds(self):
        def fake(url, headers=None):
            if url.endswith("/projects"):
                return {"projects": [{"project_id": "p1"}]}
            return {"results": [{"total_hours": 0.5}, {"hours": 0.25}]}

        with mock.patch.object(rc, "_get_json", fake):
            report = rc.fetch_deepgram("2026-09-27", "k")
        self.assertAlmostEqual(report.units["audio_input_seconds"], 2700.0)

    def test_network_error_returns_none(self):
        def boom(*_a, **_k):
            raise TimeoutError("slow")

        with mock.patch.object(rc, "_get_json", boom):
            self.assertIsNone(rc.fetch_elevenlabs("2026-09-27", "k"))


if __name__ == "__main__":
    unittest.main()
