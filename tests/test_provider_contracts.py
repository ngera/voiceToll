"""Contract tests: replay pinned provider usage-API responses through the reconciliation connectors.

`tests/fixtures/provider_usage/docs/*.json` hold shapes from the providers' API references;
`tests/fixtures/provider_usage/live/*.json` hold real responses captured with `voicetoll-collector recon-capture`.
"""

from __future__ import annotations

import json
import unittest
import urllib.parse
from pathlib import Path

from voicetoll_collector.reconcile import ReconAccount, _Redactor, capture_fixtures, replay_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "provider_usage"


def _fixtures(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("*.json"))


class ProviderContractTests(unittest.TestCase):
    def check(self, path: Path) -> None:
        fx = json.loads(path.read_text())
        report, urls = replay_fetch(fx["provider"], fx["day"], fx["options"], fx["responses"])
        self.assertIsNotNone(report, f"{path.name}: connector returned nothing")
        expected = fx["expected"]
        if expected.get("usd") is None:
            self.assertIsNone(report.usd, path.name)
        else:
            self.assertAlmostEqual(report.usd, expected["usd"], places=8, msg=path.name)
        self.assertEqual(set(report.units), set(expected.get("units") or {}), path.name)
        for unit, value in (expected.get("units") or {}).items():
            self.assertAlmostEqual(report.units[unit], value, places=6, msg=f"{path.name} {unit}")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(urls[-1]).query))
        for key, value in (fx.get("expected_query") or {}).items():
            self.assertEqual(query.get(key), value, f"{path.name}: query {key}")

    def test_documented_shapes(self):
        files = _fixtures("docs")
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            with self.subTest(fixture=path.name):
                self.check(path)

    def test_captured_live_shapes(self):
        files = _fixtures("live")
        if not files:
            self.skipTest("no live captures yet: run `voicetoll-collector recon-capture` after the first G3 day")
        for path in files:
            with self.subTest(fixture=path.name):
                self.check(path)


class CaptureTests(unittest.TestCase):
    def test_redactor_is_stable_and_keeps_numbers(self):
        r = _Redactor()
        body = {"data": [{"project_id": "proj_abc", "amount": {"value": 1.5}}, {"project_id": "proj_abc"}]}
        out = r.walk(body)
        self.assertEqual(out["data"][0]["project_id"], out["data"][1]["project_id"])
        self.assertTrue(out["data"][0]["project_id"].startswith("proj_REDACTED_"))
        self.assertEqual(out["data"][0]["amount"]["value"], 1.5)

    def test_capture_writes_redacted_fixture_that_replays(self):
        import os
        import tempfile
        from unittest import mock

        from voicetoll_collector import reconcile as rc

        fx = json.loads((FIXTURES / "docs" / "deepgram.json").read_text())

        def fake_urlopen_json(url, headers=None):
            body = next(i["body"] for i in fx["responses"] if urllib.parse.urlparse(url).path == i["path"])
            if rc.RESPONSE_RECORDER:
                rc.RESPONSE_RECORDER(url, body)
            return body

        account = ReconAccount(project="demo", provider="deepgram", key_env="VT_TEST_DG",
                               options={"deepgram_project_id": "dg-project"})
        out = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"VT_TEST_DG": "k"}), mock.patch.object(rc, "_get_json", fake_urlopen_json):
            result = capture_fixtures([account], fx["day"], out)
        self.assertEqual(result[0]["status"], "saved")
        saved = json.loads(Path(result[0]["path"]).read_text())
        self.assertNotIn("dg-project", json.dumps(saved))  # the project id is redacted, in paths too
        self.assertEqual(saved["expected"]["units"], {"audio_input_seconds": 1800.0})
        report, _ = replay_fetch("deepgram", saved["day"], saved["options"], saved["responses"])
        self.assertEqual(report.units, saved["expected"]["units"])


if __name__ == "__main__":
    unittest.main()
