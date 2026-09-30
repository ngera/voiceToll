"""`voicetoll-collector doctor`: check the setup and say exactly what to fix.

voiceToll's per-call dollars need no provider keys. Keys are only for the optional check against each
provider's own usage data (daily reconciliation and the per-call audit). This command tests every key the way
reconciliation will use it, finds the provider-side project for you, names the missing permission with a link,
and with --write saves the result to the reconciliation config.

Read-only calls only. Key values are never printed or written: the config stores environment variable names.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from . import reconcile
from .audit import _http_note
from .reconcile import AGENT_KEY_ENV, ReconAccount, load_accounts

CONNECTORS = ("openai", "elevenlabs", "deepgram")
KEY_PAGES = {
    "openai": "https://platform.openai.com/settings/organization/admin-keys",
    "elevenlabs": "https://elevenlabs.io/app/settings/api-keys",
    "deepgram": "https://console.deepgram.com/",
}
DEFAULT_CONFIG = "config/reconcile.yaml"


@dataclass
class Check:
    name: str
    state: str  # ok | warn | fail | info
    message: str
    fix: str | None = None


@dataclass
class ProviderResult:
    provider: str
    key_env: str | None
    agent_key: bool
    checks: list[Check] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    dedicated: bool = True

    @property
    def usable(self) -> bool:
        return self.key_env is not None and not any(c.state == "fail" for c in self.checks)


def _recon_env(provider: str) -> str:
    return f"VOICETOLL_RECON_{provider.upper()}_KEY"


def _find_key(provider: str, configured: ReconAccount | None) -> tuple[str | None, bool]:
    """(environment variable holding the key, is it the agent's own key)."""
    names = [configured.key_env] if configured and configured.key_env else []
    names.append(_recon_env(provider))
    for name in names:
        if os.environ.get(name):
            return name, False
    for name in AGENT_KEY_ENV.get(provider, ()):
        if os.environ.get(name):
            return name, True
    return None, False


def _code(exc: Exception) -> int | None:
    return exc.code if isinstance(exc, urllib.error.HTTPError) else None


# ---- providers ------------------------------------------------------------------------------
def check_deepgram(key: str, options: dict[str, Any], http: Any) -> tuple[list[Check], dict[str, Any], bool]:
    headers = {"Authorization": f"Token {key}"}
    checks: list[Check] = []
    try:
        projects = http("https://api.deepgram.com/v1/projects", headers).get("projects") or []
    except Exception as exc:
        fix = "Create a key in the Deepgram console (Settings > API Keys) with the Member role or usage:read."
        return [Check("Key", "fail", f"Deepgram rejected the key ({_http_note(exc)})", fix)], options, True
    names = {p.get("project_id"): p.get("name") for p in projects}
    wanted = options.get("deepgram_project_id")
    if wanted and wanted not in names:
        listed = ", ".join(f"{n} ({i})" for i, n in names.items()) or "none"
        checks.append(
            Check(
                "Project",
                "fail",
                f"the configured project {wanted} is not one this key can see",
                f"Use one of the key's projects: {listed}, or a key from project {wanted}.",
            )
        )
        return checks, options, True
    if not wanted:
        if not names:
            return [Check("Project", "fail", "this key sees no Deepgram project", None)], options, True
        wanted = next(iter(names))
        note = f"found project {names[wanted]} ({wanted})"
        if len(names) > 1:
            note += (
                f"; the key also sees {len(names) - 1} other project(s), set deepgram_project_id to choose"
            )
        checks.append(Check("Project", "ok" if len(names) == 1 else "warn", note))
    else:
        checks.append(Check("Project", "ok", f"{names[wanted]} ({wanted})"))
    options = {**options, "deepgram_project_id": wanted}
    day = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    try:
        http(
            f"https://api.deepgram.com/v1/projects/{wanted}/usage?"
            + urllib.parse.urlencode({"start": day, "end": day}),
            headers,
        )
        checks.append(Check("Daily usage", "ok", "readable (daily reconciliation works)"))
    except Exception as exc:
        checks.append(
            Check(
                "Daily usage",
                "fail",
                f"not readable ({_http_note(exc)})",
                "Give the key the Member role, or the usage:read scope.",
            )
        )
    try:
        http(
            f"https://api.deepgram.com/v1/projects/{wanted}/requests?"
            + urllib.parse.urlencode({"start": day, "end": day, "limit": 1}),
            headers,
        )
        checks.append(Check("Request log", "ok", "readable (per-call audit works)"))
    except Exception as exc:
        checks.append(
            Check(
                "Request log",
                "warn",
                f"not readable ({_http_note(exc)}); the per-call audit will skip Deepgram",
                "Give the key the Member role, or the usage:read scope.",
            )
        )
    return checks, options, True


def check_elevenlabs(
    key: str, options: dict[str, Any], http: Any
) -> tuple[list[Check], dict[str, Any], bool]:
    headers = {"xi-api-key": key}
    checks: list[Check] = []
    now = datetime.now(tz=UTC)
    params = {
        "start_unix": int((now - timedelta(days=1)).timestamp() * 1000),
        "end_unix": int(now.timestamp() * 1000),
        "metric": "tts_characters",
        "aggregation_interval": "cumulative",
        "breakdown_type": "none",
    }
    try:
        http("https://api.elevenlabs.io/v1/usage/character-stats?" + urllib.parse.urlencode(params), headers)
        checks.append(Check("Character usage", "ok", "readable (daily reconciliation works)"))
    except Exception as exc:
        fix = (
            "Edit the key in ElevenLabs (Developers > API Keys) and allow reading usage; "
            "an unrestricted key also works."
        )
        state = "fail" if _code(exc) in (401, 403) else "warn"
        checks.append(Check("Character usage", state, f"not readable ({_http_note(exc)})", fix))
    try:
        http("https://api.elevenlabs.io/v1/history?page_size=1", headers)
        checks.append(Check("Speech history", "ok", "readable (per-call audit works)"))
    except Exception as exc:
        checks.append(
            Check(
                "Speech history",
                "warn",
                f"not readable ({_http_note(exc)}); the per-call audit will skip ElevenLabs",
                "Edit the key and turn on Speech History > Read.",
            )
        )
    return checks, options, True


def _openai_agent_project(projects: list[dict[str, Any]], headers: dict[str, str], http: Any) -> str | None:
    """The OpenAI project holding the agent's key (OPENAI_API_KEY), matched on its redacted form."""
    agent = os.environ.get("OPENAI_API_KEY") or ""
    if len(agent) < 12:
        return None
    for proj in projects[:25]:
        pid = proj.get("id")
        try:
            keys = http(f"https://api.openai.com/v1/organization/projects/{pid}/api_keys?limit=100", headers)
        except Exception:
            continue
        for k in keys.get("data") or []:
            redacted = str(k.get("redacted_value") or "")
            if "..." not in redacted and "***" not in redacted:
                continue
            sep = "..." if "..." in redacted else "***"
            head, tail = redacted.split(sep, 1)
            if head and tail and agent.startswith(head) and agent.endswith(tail):
                return pid
    return None


def check_openai(key: str, options: dict[str, Any], http: Any) -> tuple[list[Check], dict[str, Any], bool]:
    headers = {"Authorization": f"Bearer {key}"}
    checks: list[Check] = []
    try:
        projects = (
            http("https://api.openai.com/v1/organization/projects?limit=100", headers).get("data") or []
        )
    except Exception as exc:
        fix = f"OpenAI's usage and costs need an organization admin key: {KEY_PAGES['openai']}"
        return (
            [Check("Admin key", "fail", f"not an admin key or rejected ({_http_note(exc)})", fix)],
            options,
            True,
        )
    checks.append(Check("Admin key", "ok", f"sees {len(projects)} project(s)"))
    ids = {p.get("id"): p.get("name") for p in projects}
    wanted = options.get("openai_project_id")
    dedicated = True
    if wanted and wanted not in ids:
        checks.append(
            Check(
                "Project",
                "fail",
                f"the configured project {wanted} is not in this organization",
                "Remove openai_project_id or set it to one of: " + ", ".join(ids),
            )
        )
    elif wanted:
        checks.append(Check("Project", "ok", f"{ids[wanted]} ({wanted})"))
    else:
        found = _openai_agent_project(projects, headers, http)
        if found:
            options = {**options, "openai_project_id": found}
            checks.append(Check("Project", "ok", f"the agent's key is in project {ids.get(found)} ({found})"))
        else:
            dedicated = False
            checks.append(
                Check(
                    "Project",
                    "warn",
                    "could not tell which project the agent's key belongs to, so "
                    "the whole organization is compared (scope: shared, never alerts)",
                    "Set openai_project_id to the project your voice agents use.",
                )
            )
    now = datetime.now(tz=UTC)
    try:
        http(
            "https://api.openai.com/v1/organization/costs?"
            + urllib.parse.urlencode({"start_time": int((now - timedelta(days=1)).timestamp()), "limit": 1}),
            headers,
        )
        checks.append(Check("Costs", "ok", "readable (daily reconciliation works)"))
    except Exception as exc:
        checks.append(
            Check("Costs", "fail", f"not readable ({_http_note(exc)})", "Use an admin key with read access.")
        )
    try:
        http(
            "https://api.openai.com/v1/organization/usage/completions?"
            + urllib.parse.urlencode(
                {"start_time": int((now - timedelta(hours=1)).timestamp()), "bucket_width": "1m", "limit": 1}
            ),
            headers,
        )
        checks.append(Check("Token usage", "ok", "readable (per-call audit works)"))
    except Exception as exc:
        checks.append(Check("Token usage", "warn", f"not readable ({_http_note(exc)})", None))
    return checks, options, dedicated


CHECKERS = {"openai": check_openai, "elevenlabs": check_elevenlabs, "deepgram": check_deepgram}


# ---- whole setup ----------------------------------------------------------------------------
def run_doctor(
    store: Any,
    pricer: Any,
    project: str | None = None,
    http: Any = None,
    config_path: str | None = None,
    offline: bool = False,
) -> dict[str, Any]:
    http = http or reconcile._get_json
    config_path = config_path if config_path is not None else os.environ.get("VOICETOLL_RECON_CONFIG")
    setup: list[Check] = []

    try:
        store.ping()
        setup.append(Check("Database", "ok", "reachable"))
    except Exception as exc:
        setup.append(
            Check("Database", "fail", f"not reachable ({type(exc).__name__})", "Check VOICETOLL_DB_URL.")
        )
    since = datetime.now(tz=UTC).timestamp() - 30 * 86_400
    seen = store._query(
        "SELECT project_id, provider, COUNT(*) AS n FROM usage_event WHERE ts_epoch >= ? GROUP BY project_id, provider",
        (since,),
    )
    projects = sorted({r["project_id"] for r in seen})
    if not seen:
        setup.append(
            Check(
                "Events",
                "warn",
                "no events in the last 30 days",
                "Start an agent with the voiceToll client and make a call; see README step 1.",
            )
        )
    else:
        setup.append(
            Check(
                "Events",
                "ok",
                f"{sum(int(r['n']) for r in seen):,} in the last 30 days from project(s) "
                + ", ".join(projects),
            )
        )
    error = store.get_state("rate_card_error") if hasattr(store, "get_state") else None
    cards = pricer.rate_cards
    if error:
        setup.append(
            Check("Rate cards", "fail", f"the file did not load: {error}", "Fix the YAML; see the error.")
        )
    else:
        setup.append(
            Check(
                "Rate cards",
                "ok" if cards.rates else "info",
                f"{len(cards.rates)} entr{'y' if len(cards.rates) == 1 else 'ies'} loaded"
                if cards.rates
                else "none (list prices from voice-prices are used; that is fine)",
            )
        )

    try:
        accounts = load_accounts(config_path)
        if config_path and Path(config_path).exists():
            setup.append(Check("Reconciliation config", "ok", f"{config_path}: {len(accounts)} account(s)"))
        else:
            setup.append(
                Check(
                    "Reconciliation config",
                    "info",
                    "not set up (optional)",
                    "Run doctor --write to create it from the checks below.",
                )
            )
    except Exception as exc:
        accounts = []
        setup.append(Check("Reconciliation config", "fail", f"{config_path} did not load: {exc}", None))

    if project is None and len(projects) == 1:
        project = projects[0]
    target = project or "default"
    used = {r["provider"] for r in seen if r["provider"] and (project is None or r["project_id"] == project)}
    configured = {a.provider: a for a in accounts if a.project == target}
    wanted = [p for p in CONNECTORS if p in used or p in configured]
    if not wanted:  # nothing seen yet: check whatever keys are present
        wanted = [p for p in CONNECTORS if _find_key(p, None)[0]]

    providers: list[ProviderResult] = []
    for provider in wanted:
        account = configured.get(provider)
        key_env, agent_key = _find_key(provider, account)
        result = ProviderResult(
            provider,
            key_env,
            agent_key,
            options=dict(account.options) if account else {},
            dedicated=account.dedicated if account else True,
        )
        if key_env is None:
            names = [_recon_env(provider), *AGENT_KEY_ENV.get(provider, ())]
            result.checks.append(
                Check(
                    "Key",
                    "info",
                    "no key, so this provider is not checked against its bill",
                    f"Set {' or '.join(names)} in .env ({KEY_PAGES[provider]}).",
                )
            )
        elif offline:
            result.checks.append(Check("Key", "info", f"found in {key_env} (not tested: --offline)"))
        else:
            if agent_key:
                result.checks.append(
                    Check(
                        "Key",
                        "info",
                        f"using the agent's key {key_env}; a separate read-only "
                        f"key in {_recon_env(provider)} is recommended",
                    )
                )
            else:
                result.checks.append(Check("Key", "info", f"found in {key_env}"))
            checks, options, dedicated = CHECKERS[provider](os.environ[key_env], result.options, http)
            result.checks += checks
            result.options = options
            result.dedicated = result.dedicated and dedicated
        providers.append(result)

    return {
        "project": target,
        "projects_seen": projects,
        "setup": setup,
        "providers": providers,
        "config_path": config_path or DEFAULT_CONFIG,
        "config_env_set": bool(config_path),
    }


def write_config(report: dict[str, Any]) -> tuple[str, list[str]]:
    """Merge the usable providers into the reconciliation config. Returns (path, provider names written)."""
    path = Path(report["config_path"])
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    existing = [a for a in data.get("accounts") or [] if isinstance(a, dict)]
    written = []
    for result in report["providers"]:
        if not result.usable:
            continue
        entry = {
            "project": report["project"],
            "provider": result.provider,
            "key_env": _recon_env(result.provider) if result.agent_key else result.key_env,
            "scope": "dedicated" if result.dedicated else "shared",
        }
        if result.options:
            entry["options"] = result.options
        existing = [
            a
            for a in existing
            if not (
                str(a.get("project")) == report["project"]
                and str(a.get("provider")).lower() == result.provider
            )
        ]
        existing.append(entry)
        written.append(result.provider)
    if not written:
        return str(path), []
    data["accounts"] = existing
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Written by `voicetoll-collector doctor --write`. Key values are never stored here, only the\n"
        "# environment variable to read. With no reconciliation key set, Deepgram and ElevenLabs fall back\n"
        "# to the agent's own key.\n"
    )
    path.write_text(header + yaml.safe_dump(data, sort_keys=False))
    return str(path), written


MARK = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "    "}


def format_report(report: dict[str, Any]) -> str:
    out = ["voiceToll setup", ""]
    for c in report["setup"]:
        out.append(f"  [{MARK[c.state]}] {c.name:<22} {c.message}")
        if c.fix and c.state != "ok":
            out.append(f"         {'':<22} -> {c.fix}")
    out += ["", f"Checking against provider bills (optional), project {report['project']}", ""]
    if not report["providers"]:
        out.append(
            "  No provider keys found. Per-call costs work without them; see README step 2 to add them."
        )
    for r in report["providers"]:
        state = "ready" if r.usable else ("not set up" if r.key_env is None else "needs a fix")
        out.append(f"  {r.provider} — {state}")
        for c in r.checks:
            out.append(f"    [{MARK[c.state]}] {c.name:<18} {c.message}")
            if c.fix and c.state != "ok":
                out.append(f"           {'':<18} -> {c.fix}")
        out.append("")
    return "\n".join(out)
