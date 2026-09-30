"""Admin UI: auth, the read-only admin views, and /report access with the admin key."""

from __future__ import annotations

import logging
import time
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from helpers import temp_dir
from starlette.testclient import TestClient
from test_collector import KEY, a_call, make_settings
from voicetoll_collector.admin import HealthMonitor
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.reconcile import ReconAccount, Report, reconcile_account

ADMIN = {"X-Voicetoll-Admin-Key": "admin-secret"}


def _rate_cards(path, reviewed: str, effective_to: str | None = None) -> str:
    lines = [
        'version: "test"',
        "rates:",
        "  - provider: elevenlabs",
        "    model: eleven_flash_v2_5",
        "    meter: characters",
        "    unit_price: 0.04",
        "    unit_size: 1000",
        f'    reviewed: "{reviewed}"',
    ]
    if effective_to:
        lines.append(f'    effective_to: "{effective_to}"')
    path.write_text("\n".join(lines) + "\n")
    return str(path)


class AdminTests(unittest.TestCase):
    def make(self, **overrides):
        settings = replace(make_settings(), admin_key="admin-secret", **overrides)
        pipeline = build_pipeline(settings)
        client = TestClient(create_app(settings, pipeline))
        now = time.time()
        for i, session in enumerate(("call-a", "call-b")):
            r = client.post(
                "/v1/events",
                json={"events": a_call(session=session, base_ts=now - 300 + i * 100)},
                headers=KEY,
            )
            self.assertEqual(r.status_code, 202)
        return settings, pipeline, client

    def setUp(self):
        self.settings, self.pipeline, self.client = self.make()

    # ---- auth ------------------------------------------------------------------------------
    def test_admin_needs_the_admin_key(self):
        self.assertEqual(self.client.get("/v1/admin/projects").status_code, 401)
        self.assertEqual(
            self.client.get("/v1/admin/projects", headers={"X-Voicetoll-Admin-Key": "nope"}).status_code, 401
        )
        self.assertEqual(
            self.client.get("/v1/admin/projects", headers=KEY).status_code, 401
        )  # ingest key is not enough
        r = self.client.get("/v1/admin/projects", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual([p["project"] for p in r.json()["projects"]], ["demo"])

    def test_admin_is_off_without_a_key_in_a_shared_deploy(self):
        settings = make_settings()  # ingest keys set, no admin key
        client = TestClient(create_app(settings, build_pipeline(settings)))
        self.assertEqual(client.get("/admin").status_code, 404)
        self.assertEqual(client.get("/v1/admin/projects", headers=ADMIN).status_code, 404)

    def test_open_mode_needs_no_key(self):
        d = temp_dir()
        settings = replace(make_settings(), ingest_keys={}, db_url=f"sqlite:///{d / 'open.db'}")
        client = TestClient(create_app(settings, build_pipeline(settings)))
        self.assertEqual(client.get("/admin").status_code, 200)
        self.assertEqual(client.get("/v1/admin/projects").status_code, 200)

    def test_admin_page_is_served_and_holds_no_data(self):
        r = self.client.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn("voiceToll admin", r.text)
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn("call-a", r.text)

    def test_report_endpoints_accept_the_admin_key_with_a_project(self):
        day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        r = self.client.get(f"/v1/calls?day={day}&project=demo", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["calls"]), 2)
        self.assertEqual(self.client.get(f"/v1/calls?day={day}&project=demo").status_code, 401)

    # ---- views -----------------------------------------------------------------------------
    def test_prices_view_lists_rates_in_use_with_tags(self):
        r = self.client.get("/v1/admin/prices?days=7&project=demo", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        meters = {(row["provider"], row["meter"]) for row in body["rows"]}
        self.assertIn(("elevenlabs", "characters"), meters)
        self.assertIn(("openai", "input_tokens"), meters)
        tts = next(
            row for row in body["rows"] if row["provider"] == "elevenlabs" and row["meter"] == "characters"
        )
        self.assertEqual(tts["price_source"], "voice_prices")
        self.assertAlmostEqual(tts["effective_price"], 0.05, places=6)  # per 1K characters
        self.assertEqual(tts["display_unit"], "per 1K chars")
        self.assertTrue(tts["tags"])
        self.assertAlmostEqual(sum(row["share"] for row in body["rows"]), 1.0, places=4)
        self.assertIn("price_version", body["versions"])
        self.assertEqual(self.client.get("/v1/admin/prices?days=x", headers=ADMIN).status_code, 400)

    def test_rate_card_review_and_expiry_tags(self):
        d = temp_dir()
        old = (date.today() - timedelta(days=200)).isoformat()
        soon = (date.today() + timedelta(days=5)).isoformat()
        _, _, client = self.make(
            rate_cards_path=_rate_cards(d / "rc.yaml", old, soon), db_url=f"sqlite:///{d / 'rc.db'}"
        )
        rows = client.get("/v1/admin/prices?project=demo", headers=ADMIN).json()["rows"]
        tts = next(r for r in rows if r["provider"] == "elevenlabs" and r["meter"] == "characters")
        self.assertEqual(tts["price_source"], "rate_card")
        texts = {t["text"] for t in tts["tags"]}
        self.assertIn("stale", texts)
        self.assertIn("expiring", texts)
        self.assertTrue(tts["stale"])
        self.assertAlmostEqual(tts["effective_price"], 0.04, places=6)
        self.assertEqual(tts["rate_card"]["reviewed"], old)

    def test_drifting_tag_from_reconciliation(self):
        day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        acct = ReconAccount(project="demo", provider="openai", key_env="")
        reconcile_account(self.pipeline.store, acct, day, fetcher=lambda *_a: Report(usd=100.0), api_key="k")
        rows = self.client.get("/v1/admin/prices?project=demo", headers=ADMIN).json()["rows"]
        llm = next(r for r in rows if r["provider"] == "openai")
        self.assertIn("drifting", {t["text"] for t in llm["tags"]})

    def test_days_view(self):
        r = self.client.get("/v1/admin/days?project=demo&days=3", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body["days"]), 3)
        self.assertEqual(body["today"]["calls"], 2)
        self.assertGreater(body["today"]["cost_usd"], 0)
        self.assertEqual(
            self.client.get("/v1/admin/days", headers=ADMIN).status_code, 400
        )  # project required

    def test_cost_view_totals_filters_and_stacking(self):
        r = self.client.get("/v1/admin/cost?days=7&project=demo", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["totals"]["calls"], 2)
        by_stage = {}
        for row in body["daily"]:
            by_stage[row["key"]] = by_stage.get(row["key"], 0) + row["cost_usd"]
        self.assertAlmostEqual(sum(by_stage.values()), body["totals"]["cost_usd"], places=6)
        self.assertIn("tts", by_stage)
        self.assertIn("elevenlabs", body["options"]["provider"])
        filtered = self.client.get(
            "/v1/admin/cost?days=7&project=demo&provider=elevenlabs", headers=ADMIN
        ).json()
        self.assertLess(filtered["totals"]["cost_usd"], body["totals"]["cost_usd"])
        self.assertEqual({r["key"] for r in filtered["by_provider"]}, {"elevenlabs"})
        by_model = self.client.get("/v1/admin/cost?days=7&by=model", headers=ADMIN).json()  # all projects
        self.assertIn("eleven_flash_v2_5", {r["key"] for r in by_model["daily"]})
        self.assertEqual(self.client.get("/v1/admin/cost?by=units_json", headers=ADMIN).status_code, 400)

    def test_health_view(self):
        logging.getLogger("voicetoll.collector").warning("test warning for the admin view")
        r = self.client.get("/v1/admin/health?minutes=15", headers=ADMIN)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        names = {c["name"]: c["state"] for c in body["checks"]}
        self.assertEqual(names["Database"], "healthy")
        self.assertEqual(names["Spool"], "healthy")
        self.assertEqual(len(body["series"]), 15)
        self.assertEqual(body["counters"]["events_accepted"], len(a_call()) * 2)
        self.assertEqual([s["project"] for s in body["sources"]], ["demo"])
        self.assertTrue(any("test warning" in w["message"] for w in body["warnings"]))

    def test_health_monitor_buckets_per_minute(self):
        m = HealthMonitor()
        counters = {"events_accepted": 0}
        m.sample(counters, now=600.0)
        counters["events_accepted"] = 7
        m.observe_request(3.0)
        m.sample(counters, now=630.0)  # same minute: nothing closed yet
        m.sample(counters, now=661.0)  # next minute closes minute 10
        series = m.series(3, now=661.0)
        self.assertEqual([b["events_accepted"] for b in series], [0, 0, 7])
        self.assertEqual(series[-1]["requests"], 1)


if __name__ == "__main__":
    unittest.main()


class CostDayRollupTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(make_settings(), admin_key="admin-secret")
        self.pipeline = build_pipeline(self.settings)
        self.client = TestClient(create_app(self.settings, self.pipeline))
        now = time.time()
        for i, session in enumerate(("r-1", "r-2", "r-3")):
            events = a_call(session=session, base_ts=now - 300 + i * 50)
            self.client.post("/v1/events", json={"events": events}, headers=KEY)
        self.client.post(
            "/v1/events", json={"events": events}, headers=KEY
        )  # duplicates must not double count
        self.day = datetime.now(tz=UTC).strftime("%Y-%m-%d")

    def raw_by_provider(self):
        return {
            r["key"]: round(r["cost_usd"], 6)
            for r in self.pipeline.store.admin_cost_by("demo", self.day, None, "provider")
        }

    def rollup_by_provider(self):
        rows = self.pipeline.store.rollup_cost_by("demo", self.day, None, "provider", {})
        return {r["key"]: round(r["cost_usd"], 6) for r in rows}

    def test_incremental_rollup_matches_raw_rows(self):
        self.assertEqual(self.rollup_by_provider(), self.raw_by_provider())
        counts = self.pipeline.store.rollup_line_counts("demo", self.day, None, {})
        raw_lines = sum(s["lines"] for s in self.pipeline.store.admin_sessions("demo", self.day))
        self.assertEqual(counts["lines"], raw_lines)

    def test_reprice_keeps_rollup_in_step(self):
        from voicetoll_collector.pricing import RateCards
        from voicetoll_collector.reprice import reprice

        d = temp_dir()
        path = d / "rc.yaml"
        path.write_text(
            'version: "x"\nrates:\n  - provider: elevenlabs\n    model: "*"\n    meter: characters\n'
            "    unit_price: 1.0\n    unit_size: 1000\n"
        )
        before = self.rollup_by_provider()["elevenlabs"]
        self.pipeline.pricer.rate_cards = RateCards.load(str(path))
        reprice(self.pipeline.store, self.pipeline.pricer, "demo")
        after = self.rollup_by_provider()
        self.assertEqual(after, self.raw_by_provider())
        self.assertAlmostEqual(after["elevenlabs"], before * 20, places=4)  # $0.05 -> $1.00 per 1K characters

    def test_rebuild_is_idempotent_and_backfill_detects_empty_rollup(self):
        store = self.pipeline.store
        expected = self.rollup_by_provider()
        store._query("DELETE FROM cost_day")
        self.assertTrue(store.cost_day_needs_backfill())
        self.assertEqual(store.rebuild_cost_days(), 1)
        store.rebuild_cost_days()
        self.assertEqual(self.rollup_by_provider(), expected)
        self.assertFalse(store.cost_day_needs_backfill())

    def test_cost_view_reads_rollups_unless_it_cannot(self):
        body = self.client.get("/v1/admin/cost?days=7&project=demo", headers=ADMIN).json()
        self.assertEqual(body["read_from"], {"calls": "call_rollup", "breakdowns": "cost_day"})
        self.assertEqual(body["totals"]["calls"], 3)
        filtered = self.client.get("/v1/admin/cost?days=7&project=demo&provider=openai", headers=ADMIN).json()
        self.assertEqual(filtered["read_from"]["calls"], "raw")
        tenant = body["tenants"][0]["tenant_id"]
        by_tenant = self.client.get(
            f"/v1/admin/cost?days=7&project=demo&tenant={tenant}", headers=ADMIN
        ).json()
        self.assertEqual(by_tenant["read_from"], {"calls": "call_rollup", "breakdowns": "raw"})
        self.assertAlmostEqual(
            by_tenant["totals"]["cost_usd"], body["totals"]["cost_usd"], places=6
        )  # one tenant


class ClientStatsTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(make_settings(), admin_key="admin-secret")
        self.client = TestClient(create_app(self.settings, build_pipeline(self.settings)))

    def post(self, client_stats):
        return self.client.post(
            "/v1/events", json={"events": a_call(session="cs-1"), "client": client_stats}, headers=KEY
        )

    def test_client_counters_are_stored_and_shown_in_health(self):
        stats = {
            "client_id": "abc123def456",
            "sdk_version": "0.1.0",
            "dropped": 7,
            "errors": 1,
            "sent": 90,
            "buffer_len": 12,
            "buffer_max": 10000,
            "started_epoch": time.time() - 60,
            "text": "never stored",
        }
        self.assertEqual(self.post(stats).status_code, 202)
        body = self.client.get("/v1/admin/health", headers=ADMIN).json()
        (row,) = body["clients"]
        self.assertEqual(
            (row["project"], row["dropped"], row["errors"], row["source"]), ("demo", 7, 1, "livekit")
        )
        self.assertNotIn("text", row)
        self.assertEqual({c["name"]: c["state"] for c in body["checks"]}["Clients"], "degraded")
        stats.update(dropped=9, sent=120)  # later batches overwrite: counters are cumulative per process
        self.post(stats)
        (row,) = self.client.get("/v1/admin/health", headers=ADMIN).json()["clients"]
        self.assertEqual(row["dropped"], 9)

    def test_bad_client_counters_are_ignored_and_never_fail_ingest(self):
        for bad in ({"client_id": "short"}, {"client_id": "abc123def456", "dropped": -1}, "nonsense"):
            self.assertEqual(self.post(bad).status_code, 202)
        self.assertEqual(self.client.get("/v1/admin/health", headers=ADMIN).json()["clients"], [])


class HighlightDayRolloverTests(unittest.TestCase):
    def test_same_finding_on_consecutive_days_does_not_collide(self):
        store = build_pipeline(make_settings()).store
        item = {"id": "hl-same-id", "rule_id": "heavy_tenant", "title": "t", "evidence": {}}
        store.save_highlights("demo", "2026-09-29", [item])
        store.save_highlights("demo", "2026-09-30", [item])  # raised IntegrityError before the fix
        rows = store.list_highlights("demo", "2026-09-01")
        self.assertEqual([(r["id"], r["day"]) for r in rows], [("hl-same-id", "2026-09-30")])
