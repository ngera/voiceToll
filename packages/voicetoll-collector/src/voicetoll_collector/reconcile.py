"""Nightly reconciliation against provider usage APIs (M4).

Compares voiceToll's estimates for one provider, project and day with what the provider itself reports,
in dollars where the provider reports dollars and in units (characters, audio seconds) where it only
reports usage.

Scope matters: a provider usage API reports a whole account or provider-side project, including traffic
that never passed through voiceToll. Accounts are therefore configured explicitly in a reconciliation
file (VOICETOLL_RECON_CONFIG), one entry per provider account mapped to one voiceToll project, and each
entry says whether that account is dedicated to the voice traffic voiceToll sees. Drift on a shared
account is recorded as informational, never as an alert.

API keys are never stored in the file: each entry names the environment variable that holds its key.
Without configured keys everything no-ops, so tests and local runs stay offline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .store import Store

log = logging.getLogger("voicetoll.collector.recon")


# ---- provider reports -----------------------------------------------------------------------
@dataclass
class Report:
    """What a provider says it billed for one day. Either part may be missing."""

    usd: float | None = None
    units: dict[str, float] = field(default_factory=dict)  # voice-prices unit names
    detail: dict[str, Any] = field(default_factory=dict)


# (day, api_key, options) -> Report or None on failure. Older fetchers returning a float (USD) still work.
Fetcher = Callable[..., "Report | float | None"]

_NET_ERRORS = (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, IndexError)


# Set by `voicetoll-collector recon-capture` to save real responses as test fixtures; None otherwise.
RESPONSE_RECORDER: Callable[[str, Any], None] | None = None


def _get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — fixed provider URLs
        data = json.loads(resp.read().decode())
    if RESPONSE_RECORDER is not None:
        RESPONSE_RECORDER(url, data)
    return data


def _day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return start, start + timedelta(days=1)


# Response shapes are pinned by tests/test_provider_contracts.py: documented examples today, captured real
# responses (tests/fixtures/provider_usage/live) once `voicetoll-collector recon-capture` has been run.


def fetch_openai(day: str, api_key: str, options: dict[str, Any] | None = None) -> Report | None:
    """OpenAI Costs API (needs an organization admin key), one daily bucket.

    With options.openai_project_id the request is filtered and grouped by project, and only results for that
    project are summed, so the bill covers only the voice traffic. Follows `next_page` if the API pages.
    """
    options = options or {}
    project = options.get("openai_project_id")
    try:
        start, end = _day_bounds(day)
        params: list[tuple[str, Any]] = [
            ("start_time", int(start.timestamp())),
            ("end_time", int(end.timestamp())),
            ("bucket_width", "1d"),
            ("limit", 1),
        ]
        if project:
            params += [("project_ids", project), ("group_by", "project_id")]
        headers = {"Authorization": f"Bearer {api_key}"}
        total, pages, page = 0.0, 0, None
        while True:
            query = params + ([("page", page)] if page else [])
            data = _get_json("https://api.openai.com/v1/organization/costs?" + urllib.parse.urlencode(query), headers)
            for bucket in data.get("data") or []:
                for result in bucket.get("results") or []:
                    if project and result.get("project_id") not in (None, project):
                        continue  # another project's cost: never part of this account's bill
                    amount = (result.get("amount") or {}).get("value")
                    if amount is not None:
                        total += float(amount)
            pages += 1
            page = data.get("next_page") if data.get("has_more") else None
            if not page or pages >= 20:
                break
        return Report(usd=total, detail={"source": "openai_costs", "pages": pages})
    except _NET_ERRORS as exc:
        log.warning("openai recon fetch failed: %s", exc)
        return None


def fetch_elevenlabs(day: str, api_key: str, options: dict[str, Any] | None = None) -> Report | None:
    """ElevenLabs TTS characters for the day (no per-day dollars, so units only).

    Asks for `metric=tts_characters` explicitly: without it the endpoint may report credits, which differ from
    characters (Flash models use half a credit per character). The endpoint is marked deprecated by
    ElevenLabs in favour of /v1/workspace/analytics/query/usage-by-product-over-time; move when it goes away.
    """
    try:
        start, end = _day_bounds(day)
        params = {
            "start_unix": int(start.timestamp() * 1000),
            "end_unix": int(end.timestamp() * 1000) - 1,
            "metric": "tts_characters",
            "aggregation_interval": "cumulative",
            "breakdown_type": "none",
        }
        url = "https://api.elevenlabs.io/v1/usage/character-stats?" + urllib.parse.urlencode(params)
        data = _get_json(url, {"xi-api-key": api_key})
        characters = 0.0
        for series in (data.get("usage") or {}).values():
            characters += sum(float(v or 0) for v in series)
        return Report(units={"characters": characters}, detail={"source": "elevenlabs_character_stats"})
    except _NET_ERRORS as exc:
        log.warning("elevenlabs recon fetch failed: %s", exc)
        return None


def fetch_deepgram(day: str, api_key: str, options: dict[str, Any] | None = None) -> Report | None:
    """Deepgram usage summary for a project, speech-to-text only: audio hours become audio_input_seconds.

    Requests start = end = the day and keeps only result rows dated that day, so it does not matter whether
    Deepgram treats `end` as inclusive. `total_hours` is compared (Deepgram describes it as including
    overhead; confirm against the invoice in the first G3 days) and `hours` is kept in the detail. Set
    options.deepgram_project_id; otherwise the key's first project is used.
    """
    options = options or {}
    headers = {"Authorization": f"Token {api_key}"}
    try:
        project_id = options.get("deepgram_project_id")
        if not project_id:
            projects = _get_json("https://api.deepgram.com/v1/projects", headers)
            project_id = ((projects.get("projects") or [{}])[0]).get("project_id")
        if not project_id:
            return None
        params = {"start": day, "end": day, "endpoint": "listen"}
        url = f"https://api.deepgram.com/v1/projects/{project_id}/usage?" + urllib.parse.urlencode(params)
        data = _get_json(url, headers)
        hours = total_hours = 0.0
        requests = 0
        for result in data.get("results") or []:
            row_day = result.get("start") or (result.get("grouping") or {}).get("start")
            if row_day and str(row_day)[:10] != day:
                continue
            h = float(result.get("hours") or 0)
            hours += h
            total_hours += float(result.get("total_hours", h) or 0)
            requests += int(result.get("requests") or 0)
        report = Report(
            units={"audio_input_seconds": total_hours * 3600.0},
            detail={"source": "deepgram_usage", "project_id": project_id, "hours": hours,
                    "total_hours": total_hours, "requests": requests},
        )
        if "total_cost" in data:
            report.usd = float(data["total_cost"])
        return report
    except _NET_ERRORS as exc:
        log.warning("deepgram recon fetch failed: %s", exc)
        return None


DEFAULT_FETCHERS: dict[str, Fetcher] = {
    "openai": fetch_openai,
    "elevenlabs": fetch_elevenlabs,
    "deepgram": fetch_deepgram,
}


# ---- configuration --------------------------------------------------------------------------
# The keys voice agents already have. Used for reconciliation only when no separate key is configured, and only
# for providers whose usage APIs accept them; a separate read-only key stays the recommended setup.
AGENT_KEY_ENV: dict[str, tuple[str, ...]] = {
    "deepgram": ("DEEPGRAM_API_KEY",),
    "elevenlabs": ("ELEVEN_API_KEY", "ELEVENLABS_API_KEY"),
}


@dataclass
class ReconAccount:
    project: str
    provider: str
    key_env: str
    dedicated: bool = True
    options: dict[str, Any] = field(default_factory=dict)

    def key_source(self) -> str | None:
        """The environment variable the key comes from: the configured one, else the agent's own key where
        that provider's usage API accepts it (Deepgram, ElevenLabs; OpenAI needs an admin key)."""
        if self.key_env and os.environ.get(self.key_env):
            return self.key_env
        for name in AGENT_KEY_ENV.get(self.provider, ()):
            if os.environ.get(name):
                return name
        return None

    def api_key(self) -> str:
        source = self.key_source()
        return os.environ.get(source, "") if source else ""

    def uses_agent_key(self) -> bool:
        source = self.key_source()
        return source is not None and source != self.key_env


def load_accounts(path: str | None) -> list[ReconAccount]:
    """Read config/reconcile.yaml. Missing file -> no accounts (reconciliation skipped)."""
    if not path or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text()) or {}
    accounts = []
    for item in data.get("accounts") or []:
        scope = str(item.get("scope", "dedicated")).lower()
        if scope not in ("dedicated", "shared"):
            raise ValueError(f"reconcile account scope must be dedicated or shared: {item}")
        accounts.append(
            ReconAccount(
                project=str(item["project"]),
                provider=str(item["provider"]).lower(),
                key_env=str(item.get("key_env") or f"VOICETOLL_RECON_{str(item['provider']).upper()}_KEY"),
                dedicated=scope == "dedicated",
                options=dict(item.get("options") or {}),
            )
        )
    return accounts


# ---- comparison -----------------------------------------------------------------------------
def _drift(estimated: float, reported: float) -> float | None:
    if reported > 0:
        return abs(estimated - reported) / reported
    if reported == 0 and estimated == 0:
        return 0.0
    return None  # provider reports zero but we estimated usage: surfaced via status, not a ratio


def _normalize(result: Report | float | None) -> Report | None:
    if result is None or isinstance(result, Report):
        return result
    return Report(usd=float(result))


def _previous_day(day: str) -> str:
    return (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def reconcile_account(
    store: Store,
    account: ReconAccount,
    day: str,
    *,
    drift_threshold: float = 0.05,
    fetcher: Fetcher | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    fetcher = fetcher or DEFAULT_FETCHERS.get(account.provider)
    key = api_key if api_key is not None else account.api_key()
    est_usd = store.estimated_provider_day(account.project, account.provider, day)
    est_units = store.estimated_provider_units(account.project, account.provider, day)

    report: Report | None = None
    if fetcher is None:
        status = "no_connector"
    elif not key:
        status = "skipped_no_key"
    else:
        try:
            report = _normalize(fetcher(day, key, account.options))
        except TypeError:  # older two-argument fetchers
            report = _normalize(fetcher(day, key))
        status = "fetch_failed" if report is None else "pending"

    usd_drift = None
    unit_drift: dict[str, float] = {}
    if report is not None:
        if report.usd is not None:
            usd_drift = _drift(est_usd, report.usd)
        for unit, reported in report.units.items():
            d = _drift(est_units.get(unit, 0.0), reported)
            if d is not None:
                unit_drift[unit] = round(d, 6)
        measured = [d for d in [usd_drift, *unit_drift.values()] if d is not None]
        if not measured:
            status = "no_reported_data" if report.usd is None and not report.units else "reported_zero"
        elif max(measured) <= drift_threshold:
            status = "ok"
        else:
            status = "drift" if account.dedicated else "drift_shared_account"

    previous = store.recon_run_for(account.project, account.provider, _previous_day(day))
    prev_consecutive = int(((previous or {}).get("detail") or {}).get("consecutive_drift_days", 0))
    consecutive = prev_consecutive + 1 if status == "drift" else 0

    row = {
        "id": hashlib.sha256(f"{account.project}|{account.provider}|{day}".encode()).hexdigest()[:20],
        "project_id": account.project,
        "provider": account.provider,
        "day": day,
        "estimated_usd": round(est_usd, 8),
        "reported_usd": None if report is None or report.usd is None else round(report.usd, 8),
        "drift_pct": None if usd_drift is None else round(usd_drift, 6),
        "status": status,
        "detail": {
            "scope": "dedicated" if account.dedicated else "shared",
            "estimated_units": {k: round(v, 3) for k, v in est_units.items()},
            "reported_units": {} if report is None else {k: round(v, 3) for k, v in report.units.items()},
            "unit_drift": unit_drift,
            "consecutive_drift_days": consecutive,
            "alert": consecutive >= 2,
            **({} if report is None else {"source": report.detail}),
        },
    }
    store.save_recon_run(row)
    if row["detail"]["alert"]:
        log.warning("reconciliation drift for %s/%s two or more days running", account.project, account.provider)
    return row


def run_reconciliation(
    store: Store,
    project: str | None,
    day: str,
    *,
    drift_threshold: float = 0.05,
    accounts: list[ReconAccount] | None = None,
    fetchers: dict[str, Fetcher] | None = None,
    api_keys: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Reconcile every configured account (optionally only those mapped to `project`).

    `fetchers` + `api_keys` without `accounts` treats each provider as a dedicated account of `project`
    (used by tests and quick manual checks).
    """
    if accounts is None:
        if api_keys and project:
            accounts = [ReconAccount(project=project, provider=p, key_env="") for p in api_keys]
        else:
            accounts = load_accounts(os.environ.get("VOICETOLL_RECON_CONFIG"))
    results = []
    for account in accounts:
        if project and account.project != project:
            continue
        results.append(
            reconcile_account(
                store,
                account,
                day,
                drift_threshold=drift_threshold,
                fetcher=(fetchers or {}).get(account.provider),
                api_key=(api_keys or {}).get(account.provider) if api_keys else None,
            )
        )
    return results


# ---- capturing real responses as test fixtures ------------------------------------------------------
_REDACT_KEYS = {
    "project_id", "api_key_id", "accessor", "id", "org_id", "organization_id", "user_id", "email", "name",
    "api_key_name", "tags", "owner",
}


class _Redactor:
    """Replaces identifiers with stable aliases (the same id always becomes the same alias)."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def alias(self, value: str) -> str:
        if value not in self.aliases:
            prefix = "proj_" if value.startswith("proj_") else ""
            self.aliases[value] = f"{prefix}REDACTED_{len(self.aliases) + 1}"
        return self.aliases[value]

    def walk(self, obj: Any, key: str | None = None) -> Any:
        if isinstance(obj, dict):
            return {k: self.walk(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str) and (key in _REDACT_KEYS or obj in self.aliases):
            return self.alias(obj)
        return obj


def replay_fetch(provider: str, day: str, options: dict[str, Any], responses: list[dict[str, Any]]) -> tuple[
    Report | None, list[str]
]:
    """Run a provider's fetcher against recorded responses (in order, matched by URL path). Returns the report
    and the URLs it requested. Used by the contract tests and by the capture command."""
    from unittest import mock

    queue = list(responses)
    urls: list[str] = []

    def fake(url: str, headers: dict[str, str] | None = None) -> Any:
        urls.append(url)
        path = urllib.parse.urlparse(url).path
        for i, item in enumerate(queue):
            if item["path"] == path or path.endswith(item["path"]):
                return queue.pop(i)["body"]
        raise ValueError(f"no recorded response for {path}")

    with mock.patch(f"{__name__}._get_json", fake):
        report = DEFAULT_FETCHERS[provider](day, "test-key", options)
    return report, urls


def capture_fixtures(accounts: list[ReconAccount], day: str, out_dir: str) -> list[dict[str, Any]]:
    """Call each configured provider for real, then save its responses (identifiers redacted) together with
    what the parser made of them. Check the printed numbers against each provider's dashboard before
    committing the files: from then on the contract tests fail if the parser reads them differently."""
    global RESPONSE_RECORDER
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for account in accounts:
        key = account.api_key()
        fetcher = DEFAULT_FETCHERS.get(account.provider)
        if not key or fetcher is None:
            written.append({"provider": account.provider, "status": "skipped (no key or connector)"})
            continue
        recorded: list[tuple[str, Any]] = []
        RESPONSE_RECORDER = lambda url, body, _rec=recorded: _rec.append((url, body))  # noqa: E731
        try:
            live = fetcher(day, key, account.options)
        finally:
            RESPONSE_RECORDER = None
        if live is None or not recorded:
            written.append({"provider": account.provider, "status": "fetch failed; see the log"})
            continue
        redactor = _Redactor()
        for value in account.options.values():  # ids from the config are redacted the same way
            if isinstance(value, str):
                redactor.alias(value)
        responses = [
            {"path": urllib.parse.urlparse(url).path, "body": redactor.walk(body)} for url, body in recorded
        ]
        # the Deepgram project id sits in the URL path; alias it there too
        for item in responses:
            for raw, alias in redactor.aliases.items():
                item["path"] = item["path"].replace(raw, alias)
        options = {k: (redactor.alias(v) if isinstance(v, str) else v) for k, v in account.options.items()}
        parsed, _urls = replay_fetch(account.provider, day, options, responses)
        fixture = {
            "provider": account.provider,
            "source": "live",
            "captured_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "day": day,
            "options": options,
            "responses": responses,
            "expected": {
                "usd": None if parsed is None else parsed.usd,
                "units": {} if parsed is None else parsed.units,
            },
        }
        path = out / f"{account.provider}.json"
        path.write_text(json.dumps(fixture, indent=2, sort_keys=True) + "\n")
        written.append({"provider": account.provider, "status": "saved", "path": str(path), **fixture["expected"]})
    return written
