"""Per-call audit: compare what voiceToll recorded for one call with each provider's itemised usage in the
call's time window.

Daily reconciliation compares whole UTC days, so any other traffic on the account that day blurs it. The audit
narrows the window to one call (its first and last event, plus padding), which makes it usable on a busy day
as long as nothing else ran at the same minute. It reads each provider's per-request log:

    ElevenLabs  GET /v1/history                               one row per generation (characters, quota counter)
    Deepgram    GET /v1/projects/{id}/requests                one row per request or stream (seconds, dollars)
    OpenAI      GET /v1/organization/usage/completions        1-minute buckets for the project (tokens)

The same reconciliation keys and accounts (config/reconcile.yaml) are used. Nothing is stored: text returned by
ElevenLabs' history is measured (its length) and dropped immediately, never logged or returned.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import reconcile
from .reconcile import ReconAccount, load_accounts
from .store import Store, _num

log = logging.getLogger("voicetoll.collector.audit")

DRIFT_THRESHOLD = 0.05
DEEPGRAM_PAGE = 100  # the request log answers limit=1000 with 400
# The unit each provider is compared in, per component voiceToll recorded
COMPARE_UNITS = {
    "elevenlabs": ("characters",),
    "cartesia": ("characters",),
    "deepgram": ("audio_input_seconds",),
    "openai": ("input_tokens", "output_tokens"),
}


@dataclass
class ProviderUsage:
    units: dict[str, float] = field(default_factory=dict)
    usd: float | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_offset(epoch: float) -> str:
    """Deepgram's request log takes YYYY-MM-DDTHH:MM:SS+HH:MM; a trailing Z is rejected with 400."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _http_note(exc: Exception) -> str:
    """Short reason for a failed provider call, shown in the audit output only (never stored)."""
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            body = json.loads(exc.read().decode(errors="replace") or "{}")
            if isinstance(body, dict):
                inner = body.get("detail") if isinstance(body.get("detail"), dict) else body
                detail = str(inner.get("err_msg") or inner.get("message") or inner.get("status") or "")[:160]
        except Exception:
            pass
        hint = {
            401: "key rejected or lacks the permission for this endpoint",
            403: "key lacks the permission for this endpoint",
        }.get(exc.code, "")
        return f"HTTP {exc.code}" + (f" ({hint})" if hint else "") + (f": {detail}" if detail else "")
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---- provider readers -----------------------------------------------------------------------
def elevenlabs_usage(start: float, end: float, api_key: str, options: dict[str, Any]) -> ProviderUsage:
    """Generation history in the window. `characters` = length of the text sent; `quota_units` = how much the
    account's character quota moved (it can differ, e.g. models that use half a credit per character)."""
    usage = ProviderUsage()
    chars = quota = 0.0
    after: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {
            "page_size": 1000,
            "date_after_unix": int(start),
            "date_before_unix": int(end) + 1,
            "sort_direction": "asc",
        }
        if after:
            params["start_after_history_item_id"] = after
        data = reconcile._get_json(
            "https://api.elevenlabs.io/v1/history?" + urllib.parse.urlencode(params), {"xi-api-key": api_key}
        )
        for item in data.get("history") or []:
            text_len = len(item.get("text") or "")  # measured, then dropped: text never leaves this function
            moved = abs(
                float(item.get("character_count_change_to") or 0)
                - float(item.get("character_count_change_from") or 0)
            )
            chars += text_len
            quota += moved
            usage.items.append(
                {
                    "time": _iso(float(item.get("date_unix") or 0)),
                    "characters": text_len,
                    "quota_units": moved,
                    "model": item.get("model_id"),
                    "ref": item.get("request_id") or item.get("history_item_id"),
                }
            )
        after = data.get("last_history_item_id")
        if not data.get("has_more") or not after:
            break
    usage.units = {"characters": chars, "quota_units": quota}
    if usage.items and abs(quota - chars) > 0.05 * max(chars, 1):
        usage.notes.append(
            f"ElevenLabs moved the quota by {quota:.0f} for {chars:.0f} characters of text "
            f"(ratio {quota / chars:.2f}); the daily character figure may be in quota units, not characters"
            if chars
            else "quota moved with no text recorded"
        )
    if not usage.items:
        usage.notes.append("no history items in the window (history can be off for zero-retention requests)")
    return usage


def deepgram_usage(start: float, end: float, api_key: str, options: dict[str, Any]) -> ProviderUsage:
    """Request log around the window: seconds of audio and dollars per request (streams count once each).

    Deepgram is asked for whole UTC days (the date-only form of `start`/`end`, which its daily usage also
    accepts) with no other filters; the exact window and the /listen endpoint are applied here, so a filter
    Deepgram reads differently cannot silently hide requests.
    """
    project = options.get("deepgram_project_id")
    if not project:
        raise ValueError("deepgram_project_id is not set for this account in the reconciliation config")
    usage = ProviderUsage()
    seconds = usd = 0.0
    have_usd = False
    fetched = 0
    nearest: float | None = None
    seen: list[str] = []  # times of every request returned, for the note when none fall in the window
    day_start = datetime.fromtimestamp(start, tz=UTC).strftime("%Y-%m-%d")
    day_end = datetime.fromtimestamp(end + 86_400, tz=UTC).strftime("%Y-%m-%d")
    for page in range(100):
        params = {"start": day_start, "end": day_end, "limit": DEEPGRAM_PAGE, "page": page}
        data = reconcile._get_json(
            f"https://api.deepgram.com/v1/projects/{project}/requests?" + urllib.parse.urlencode(params),
            {"Authorization": f"Token {api_key}"},
        )
        requests = data.get("requests") or []
        fetched += len(requests)
        for req in requests:
            created = _epoch(req.get("created"))
            if created is not None:
                seen.append(datetime.fromtimestamp(created, tz=UTC).strftime("%H:%M"))
            path = str(req.get("path") or "")
            if path and "listen" not in path:
                continue
            if created is not None and not (start <= created <= end):
                gap = min(abs(created - start), abs(created - end))
                nearest = gap if nearest is None else min(nearest, gap)
                continue
            details = ((req.get("response") or {}).get("details")) or {}
            duration = details.get("duration", details.get("total_audio"))
            dur = float(duration or 0)
            seconds += dur
            if details.get("usd") is not None:
                usd += float(details["usd"])
                have_usd = True
            usage.items.append(
                {
                    "time": req.get("created"),
                    "audio_input_seconds": round(dur, 3),
                    "usd": details.get("usd"),
                    "method": details.get("method"),
                    "ref": req.get("request_id"),
                }
            )
        if len(requests) < DEEPGRAM_PAGE:
            break
    usage.units = {"audio_input_seconds": seconds}
    usage.usd = usd if have_usd else None
    if not usage.items:
        where = f"Deepgram returned {fetched} request(s) for {day_start} to {day_end} in project {project}"
        if fetched and nearest is not None:
            where += f"; the closest was {nearest / 60:.0f} min outside the window"
            where += f"; their times (UTC): {', '.join(sorted(seen)[:20])}"
            where += (
                ". If your call is missing, Deepgram's request log has not caught up yet; try again later"
            )
        elif not fetched:
            where += "; check that deepgram_project_id is the project the agent key belongs to"
        usage.notes.append(f"no requests in the window ({where})")
    return usage


def openai_usage(start: float, end: float, api_key: str, options: dict[str, Any]) -> ProviderUsage:
    """Completion tokens in 1-minute buckets covering the window, for the configured project."""
    project = options.get("openai_project_id")
    usage = ProviderUsage()
    params: list[tuple[str, Any]] = [
        ("start_time", int(start // 60 * 60)),
        ("end_time", int(-(-end // 60) * 60)),
        ("bucket_width", "1m"),
        ("limit", 1440),
        ("group_by", "model"),
    ]
    if project:
        params += [("project_ids", project), ("group_by", "project_id")]
    headers = {"Authorization": f"Bearer {api_key}"}
    totals = {"input_tokens": 0.0, "output_tokens": 0.0, "input_cached_tokens": 0.0}
    requests = 0
    page = None
    for _ in range(20):
        query = params + ([("page", page)] if page else [])
        data = reconcile._get_json(
            "https://api.openai.com/v1/organization/usage/completions?" + urllib.parse.urlencode(query),
            headers,
        )
        for bucket in data.get("data") or []:
            for r in bucket.get("results") or []:
                if project and r.get("project_id") not in (None, project):
                    continue
                if not (r.get("input_tokens") or r.get("output_tokens")):
                    continue
                for k in totals:
                    totals[k] += float(r.get(k) or 0)
                requests += int(r.get("num_model_requests") or 0)
                usage.items.append(
                    {
                        "time": _iso(float(bucket.get("start_time") or 0)),
                        "input_tokens": r.get("input_tokens"),
                        "output_tokens": r.get("output_tokens"),
                        "model": r.get("model"),
                        "requests": r.get("num_model_requests"),
                    }
                )
        page = data.get("next_page") if data.get("has_more") else None
        if not page:
            break
    usage.units = {**totals, "requests": float(requests)}
    if not usage.items:
        usage.notes.append("no usage in the window yet (OpenAI's usage data can lag several minutes)")
    return usage


READERS = {"elevenlabs": elevenlabs_usage, "deepgram": deepgram_usage, "openai": openai_usage}


# ---- voiceToll side -------------------------------------------------------------------------
def recorded_usage(store: Store, project: str, session: str) -> dict[str, Any] | None:
    """What voiceToll recorded for one call: window, and units and dollars per provider."""
    events = store._query(
        "SELECT event_id, ts_epoch, component, provider, model, units_json FROM usage_event "
        "WHERE project_id = ? AND session_id = ? ORDER BY ts_epoch",
        (project, session),
    )
    if not events:
        return None
    lines = store.current_lines_by_event([e["event_id"] for e in events])
    providers: dict[str, dict[str, Any]] = {}
    for e in events:
        if not e["provider"]:
            continue
        p = providers.setdefault(e["provider"], {"units": {}, "usd": 0.0, "events": 0, "models": set()})
        p["events"] += 1
        if e["model"]:
            p["models"].add(e["model"])
        for unit, v in (json.loads(e["units_json"] or "{}")).items():
            p["units"][unit] = p["units"].get(unit, 0.0) + float(v.get("v", 0) if isinstance(v, dict) else v)
        p["usd"] += sum(_num(line["amount_usd"]) for line in lines.get(e["event_id"], []))
    for p in providers.values():
        p["models"] = sorted(p["models"])
        p["usd"] = round(p["usd"], 8)
        p["units"] = {k: round(v, 3) for k, v in p["units"].items()}
    return {
        "first_event": float(events[0]["ts_epoch"]),
        "last_event": float(events[-1]["ts_epoch"]),
        "events": len(events),
        "providers": providers,
    }


# ---- audit ----------------------------------------------------------------------------------
def audit_call(
    store: Store,
    project: str,
    session: str,
    *,
    pad_before: float = 120.0,
    pad_after: float = 60.0,
    accounts: list[ReconAccount] | None = None,
    readers: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    recorded = recorded_usage(store, project, session)
    if recorded is None:
        return None
    start, end = recorded["first_event"] - pad_before, recorded["last_event"] + pad_after
    if accounts is None:
        accounts = load_accounts(os.environ.get("VOICETOLL_RECON_CONFIG"))
    by_provider = {a.provider: a for a in accounts if a.project == project}
    readers = readers or READERS
    results = []
    for provider, rec in sorted(recorded["providers"].items()):
        row: dict[str, Any] = {
            "provider": provider,
            "recorded": rec,
            "reported": None,
            "comparison": {},
            "notes": [],
        }
        account = by_provider.get(provider)
        reader = readers.get(provider)
        if reader is None:
            row["status"] = "no_connector"
        elif account is None:
            row["status"] = "no_account"
            row["notes"].append(f"no {provider} account for project {project} in the reconciliation config")
        elif not account.api_key():
            row["status"] = "skipped_no_key"
        else:
            try:
                reported = reader(start, end, account.api_key(), account.options)
            except Exception as exc:  # a provider error must not break the audit of the others
                row["status"] = "fetch_failed"
                row["notes"].append(_http_note(exc))
                if provider == "elevenlabs" and "401" in row["notes"][-1]:
                    row["notes"].append(
                        "the ElevenLabs recon key needs read access to Speech History (/v1/history)"
                    )
            else:
                row["reported"] = {
                    "units": {k: round(v, 3) for k, v in reported.units.items()},
                    "usd": reported.usd,
                    "items": reported.items,
                }
                row["notes"] += reported.notes
                worst = 0.0
                for unit in COMPARE_UNITS.get(provider, ()):
                    ours = float(rec["units"].get(unit, 0.0))
                    theirs = float(reported.units.get(unit, 0.0))
                    drift = abs(ours - theirs) / theirs if theirs else (0.0 if ours == 0 else None)
                    row["comparison"][unit] = {
                        "voicetoll": round(ours, 3),
                        "provider": round(theirs, 3),
                        "drift": None if drift is None else round(drift, 4),
                    }
                    worst = max(worst, drift if drift is not None else 1.0)
                if reported.usd is not None:
                    theirs = reported.usd
                    row["comparison"]["usd"] = {
                        "voicetoll": rec["usd"],
                        "provider": round(theirs, 8),
                        "drift": round(abs(rec["usd"] - theirs) / theirs, 4) if theirs else None,
                    }
                if not reported.items:
                    row["status"] = "no_provider_data"
                else:
                    row["status"] = "ok" if worst <= DRIFT_THRESHOLD else "drift"
                if account is not None and not account.dedicated:
                    row["notes"].append("shared account: other traffic in the same minutes is included")
        results.append(row)
    return {
        "project": project,
        "session": session,
        "window": {
            "start": _iso(start),
            "end": _iso(end),
            "first_event": _iso(recorded["first_event"]),
            "last_event": _iso(recorded["last_event"]),
            "pad_before_s": pad_before,
            "pad_after_s": pad_after,
        },
        "events": recorded["events"],
        "providers": results,
        "caveat": "Anything else using these provider accounts inside the window is counted too; leave a few "
        "minutes between test calls.",
    }


def format_audit(result: dict[str, Any]) -> str:
    """Plain-text table for the CLI."""
    w = result["window"]
    out = [
        f"Call {result['session']} (project {result['project']}), {result['events']} events",
        f"Window {w['start']} to {w['end']} (first event {w['first_event']}, last {w['last_event']})",
        "",
    ]
    for row in result["providers"]:
        out.append(f"{row['provider']:<11} {row['status'].upper()}")
        for unit, c in row["comparison"].items():
            drift = "—" if c["drift"] is None else f"{c['drift'] * 100:.1f}%"
            out.append(
                f"   {unit:<20} voiceToll {c['voicetoll']:>12,.3f}   provider {c['provider']:>12,.3f}   gap {drift}"
            )
        reported = row.get("reported") or {}
        if reported.get("items"):
            out.append(f"   provider items: {len(reported['items'])}")
            for item in reported["items"][:12]:
                fields = ", ".join(f"{k}={v}" for k, v in item.items() if k not in ("ref",) and v is not None)
                out.append(f"     - {fields}")
        for note in row["notes"]:
            out.append(f"   note: {note}")
        out.append("")
    out.append(result["caveat"])
    return "\n".join(out)
