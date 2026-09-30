"""`voicetoll-collector doctor` (doctor.py): offline, provider APIs replaced by canned responses."""

from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import yaml
from test_collector import make_settings
from voicetoll_collector.app import build_pipeline
from voicetoll_collector.doctor import format_report, run_doctor, write_config
from voicetoll_collector.reconcile import ReconAccount, load_accounts


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"{}"))


def provider_api(history_ok=True, openai_admin=True):
    def http(url, headers=None):
        if "api.deepgram.com/v1/projects?" in url or url.endswith("api.deepgram.com/v1/projects"):
            return {"projects": [{"project_id": "dg-proj-1", "name": "voice"}]}
        if "api.deepgram.com" in url:
            return {"results": [], "requests": []}
        if "character-stats" in url:
            return {"usage": {"All": [10]}}
        if "elevenlabs.io/v1/history" in url:
            if not history_ok:
                raise _http_error(401)
            return {"history": []}
        if "organization/projects?" in url:
            if not openai_admin:
                raise _http_error(403)
            return {"data": [{"id": "proj_other", "name": "Other"}, {"id": "proj_voice", "name": "Voice"}]}
        if "/api_keys" in url:
            if "proj_voice" in url:
                return {"data": [{"redacted_value": "sk-proj-abc...wxyz"}]}
            return {"data": [{"redacted_value": "sk-proj-zzz...0000"}]}
        if "organization/costs" in url or "usage/completions" in url:
            return {"data": []}
        raise AssertionError(url)

    return http


ENV = {
    "DEEPGRAM_API_KEY": "dg-agent-key",
    "ELEVEN_API_KEY": "el-agent-key",
    "OPENAI_API_KEY": "sk-proj-abc123456789wxyz",
    "VOICETOLL_RECON_OPENAI_KEY": "sk-admin-123",
}


def _event(n, provider, model, units):
    return {
        "event_id": f"evt-doc-{n:07d}",
        "ts": time.time() - 60,
        "session": "c1",
        "component": "tts",
        "provider": provider,
        "model": model,
        "units": units,
    }


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.pipeline = build_pipeline(make_settings())
        self.pipeline.ingest(
            [
                _event(1, "deepgram", "nova-3", {"audio_input_seconds": 5}),
                _event(2, "elevenlabs", "eleven_flash_v2_5", {"characters": 50}),
                _event(3, "openai", "gpt-4o-mini", {"input_tokens": 10}),
            ],
            "demo",
        )
        self.config = str(Path(tempfile.mkdtemp()) / "config" / "reconcile.yaml")
        env = mock.patch.dict(os.environ, ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("VOICETOLL_RECON_DEEPGRAM_KEY", "VOICETOLL_RECON_ELEVENLABS_KEY", "ELEVENLABS_API_KEY"):
            os.environ.pop(name, None)

    def run_doctor(self, **kw):
        kw.setdefault("http", provider_api())
        return run_doctor(self.pipeline.store, self.pipeline.pricer, config_path=self.config, **kw)

    def by_provider(self, report):
        return {r.provider: r for r in report["providers"]}

    def test_finds_projects_and_uses_agent_keys_where_they_work(self):
        report = self.run_doctor()
        self.assertEqual(report["project"], "demo")
        p = self.by_provider(report)
        self.assertTrue(all(r.usable for r in p.values()))
        self.assertEqual(p["deepgram"].options["deepgram_project_id"], "dg-proj-1")
        self.assertTrue(p["deepgram"].agent_key)
        self.assertEqual(
            p["openai"].options["openai_project_id"], "proj_voice"
        )  # matched on the redacted key
        self.assertEqual(p["openai"].key_env, "VOICETOLL_RECON_OPENAI_KEY")

    def test_missing_permission_names_the_fix(self):
        report = self.run_doctor(http=provider_api(history_ok=False))
        el = self.by_provider(report)["elevenlabs"]
        history = next(c for c in el.checks if c.name == "Speech history")
        self.assertEqual(history.state, "warn")
        self.assertIn("Speech History", history.fix)
        self.assertTrue(el.usable)  # the daily check still works without history

    def test_openai_without_an_admin_key_fails_with_a_link(self):
        report = self.run_doctor(http=provider_api(openai_admin=False))
        oa = self.by_provider(report)["openai"]
        self.assertFalse(oa.usable)
        self.assertIn("admin-keys", oa.checks[-1].fix)
        self.assertIn("FAIL", format_report(report))

    def test_key_values_never_appear_in_output_or_config(self):
        report = self.run_doctor()
        text = format_report(report)
        path, written = write_config(report)
        saved = Path(path).read_text()
        for value in ENV.values():
            self.assertNotIn(value, text)
            self.assertNotIn(value, saved)
        self.assertEqual(sorted(written), ["deepgram", "elevenlabs", "openai"])

    def test_written_config_loads_and_falls_back_to_agent_keys(self):
        path, _ = write_config(self.run_doctor())
        accounts = {a.provider: a for a in load_accounts(path)}
        self.assertEqual(accounts["deepgram"].key_env, "VOICETOLL_RECON_DEEPGRAM_KEY")
        self.assertEqual(accounts["deepgram"].api_key(), "dg-agent-key")
        self.assertTrue(accounts["deepgram"].uses_agent_key())
        self.assertFalse(accounts["openai"].uses_agent_key())
        self.assertEqual(accounts["deepgram"].options["deepgram_project_id"], "dg-proj-1")

    def test_write_keeps_other_accounts(self):
        Path(self.config).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config).write_text(
            yaml.safe_dump({"accounts": [{"project": "other", "provider": "deepgram", "key_env": "X_KEY"}]})
        )
        write_config(self.run_doctor())
        projects = {
            (a["project"], a["provider"]) for a in yaml.safe_load(Path(self.config).read_text())["accounts"]
        }
        self.assertIn(("other", "deepgram"), projects)
        self.assertIn(("demo", "deepgram"), projects)

    def test_configured_project_the_key_cannot_see_fails(self):
        Path(self.config).parent.mkdir(parents=True, exist_ok=True)
        Path(self.config).write_text(
            yaml.safe_dump(
                {
                    "accounts": [
                        {
                            "project": "demo",
                            "provider": "deepgram",
                            "options": {"deepgram_project_id": "wrong"},
                        }
                    ]
                }
            )
        )
        dg = self.by_provider(self.run_doctor())["deepgram"]
        self.assertFalse(dg.usable)
        self.assertIn("dg-proj-1", next(c for c in dg.checks if c.name == "Project").fix)

    def test_offline_makes_no_calls(self):
        def boom(*a, **k):
            raise AssertionError("no network in --offline")

        report = self.run_doctor(http=boom, offline=True)
        self.assertEqual(len(report["providers"]), 3)

    def test_agent_key_fallback_is_not_used_for_openai(self):
        account = ReconAccount(project="demo", provider="openai", key_env="VOICETOLL_RECON_OPENAI_KEY")
        with mock.patch.dict(os.environ, {"VOICETOLL_RECON_OPENAI_KEY": ""}):
            self.assertEqual(account.api_key(), "")


if __name__ == "__main__":
    unittest.main()
