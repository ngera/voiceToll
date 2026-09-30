"""Built-in report: list-calls and recon endpoints, and the /report page."""

from __future__ import annotations

import time
import unittest
from datetime import UTC, datetime

from starlette.testclient import TestClient
from test_collector import KEY, a_call, make_settings
from voicetoll_collector.app import build_pipeline, create_app
from voicetoll_collector.reconcile import ReconAccount, Report, reconcile_account


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.settings = make_settings()
        self.pipeline = build_pipeline(self.settings)
        self.client = TestClient(create_app(self.settings, self.pipeline))
        now = time.time()
        self.day = datetime.fromtimestamp(now - 300, tz=UTC).strftime("%Y-%m-%d")
        for i, session in enumerate(("call-a", "call-b")):
            r = self.client.post("/v1/events", json={"events": a_call(session=session, base_ts=now - 300 + i * 100)},
                                 headers=KEY)
            self.assertEqual(r.status_code, 202)

    def test_list_calls(self):
        r = self.client.get(f"/v1/calls?day={self.day}", headers=KEY)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["project"], "demo")
        self.assertEqual([c["session_id"] for c in body["calls"]], ["call-b", "call-a"])  # newest first
        call = body["calls"][0]
        self.assertEqual(call["turns"], 2)
        self.assertGreater(call["cost_usd"], 0)
        self.assertGreater(call["minutes"], 0)
        # cost per call matches the session summary
        summary = self.client.get("/v1/sessions/call-b", headers=KEY).json()
        self.assertAlmostEqual(call["cost_usd"], summary["cost_usd"], places=6)

    def test_list_calls_other_day_and_auth(self):
        self.assertEqual(self.client.get("/v1/calls?day=2000-01-01", headers=KEY).json()["calls"], [])
        self.assertEqual(self.client.get(f"/v1/calls?day={self.day}").status_code, 401)
        self.assertEqual(self.client.get("/v1/calls?limit=x", headers=KEY).status_code, 400)

    def test_recon_endpoint(self):
        acct = ReconAccount(project="demo", provider="openai", key_env="")
        reconcile_account(self.pipeline.store, acct, self.day, fetcher=lambda *_a: Report(usd=1.0), api_key="k")
        r = self.client.get("/v1/recon?days=3", headers=KEY)
        self.assertEqual(r.status_code, 200)
        runs = r.json()["runs"]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["provider"], "openai")
        self.assertEqual(runs[0]["reported_usd"], 1.0)
        self.assertIn("scope", runs[0]["detail"])

    def test_provider_host_names_are_stored_as_provider_names(self):
        events = a_call(session="call-host")
        for e in events:
            if e.get("provider") == "openai":
                e["provider"] = "api.openai.com"
        self.client.post("/v1/events", json={"events": events}, headers=KEY)
        summary = self.client.get("/v1/sessions/call-host", headers=KEY).json()
        self.assertEqual(summary["components"]["llm"]["provider"], "openai")

    def test_report_page_is_static_html(self):
        r = self.client.get("/report")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("voiceToll", r.text)
        self.assertNotIn("call-a", r.text)  # the page carries no data
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/report")


if __name__ == "__main__":
    unittest.main()
