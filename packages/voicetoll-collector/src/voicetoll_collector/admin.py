"""Admin UI back end: collector health bookkeeping and the read-only views behind /v1/admin/*.

Everything here is read-only and works across projects. Auth (the admin key) is enforced in app.py.
Health series are kept in memory per replica: they reset on restart and show only this replica.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from collections import deque
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .settings import Settings

SERIES_COUNTERS = (
    "events_accepted",
    "events_rejected",
    "events_duplicate",
    "events_spooled",
    "events_replayed",
)
STALE_STATUSES = ("stale",)
UNVERIFIED_STATUSES = ("seed", "imported")


# ---- recent warnings ------------------------------------------------------------------------------
class RecentWarnings(logging.Handler):
    """Keeps the last N collector log records at WARNING or above, for the Health view."""

    def __init__(self, capacity: int = 100) -> None:
        super().__init__(level=logging.WARNING)
        self.records: deque[dict[str, Any]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                {
                    "epoch": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage()[:500],
                }
            )
        except Exception:  # logging must never raise
            pass


_WARNINGS: RecentWarnings | None = None
_WARNINGS_LOCK = threading.Lock()


def recent_warnings() -> RecentWarnings:
    """One handler per process on the `voicetoll` logger, however many apps are created (tests make many)."""
    global _WARNINGS
    with _WARNINGS_LOCK:
        if _WARNINGS is None:
            _WARNINGS = RecentWarnings()
            logging.getLogger("voicetoll").addHandler(_WARNINGS)
        return _WARNINGS


# ---- health monitor -------------------------------------------------------------------------------
class HealthMonitor:
    """Per-minute counter deltas and ingest request timings, plus the last run of each background loop."""

    def __init__(self, minutes: int = 1440) -> None:
        self.started_epoch = time.time()
        self.replica = os.environ.get("HOSTNAME") or os.environ.get("COMPUTERNAME") or "local"
        self._lock = threading.Lock()
        self._buckets: deque[dict[str, Any]] = deque(maxlen=minutes)
        self._minute: int | None = None
        self._base: dict[str, int] = {}
        self._durations: list[float] = []
        self.loops: dict[str, dict[str, Any]] = {}

    def sample(self, counters: dict[str, int], now: float | None = None) -> None:
        """Close the current minute when the clock has moved on. Called on ingest and on health reads."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        with self._lock:
            if self._minute is None:
                self._minute, self._base = minute, {k: int(counters.get(k, 0)) for k in SERIES_COUNTERS}
                return
            if minute == self._minute:
                return
            bucket = {k: int(counters.get(k, 0)) - self._base.get(k, 0) for k in SERIES_COUNTERS}
            bucket["minute"] = self._minute
            bucket["requests"] = len(self._durations)
            bucket["p50_ms"] = _quantile(self._durations, 0.50)
            bucket["p95_ms"] = _quantile(self._durations, 0.95)
            self._buckets.append(bucket)
            self._minute, self._base, self._durations = (
                minute,
                {k: int(counters.get(k, 0)) for k in SERIES_COUNTERS},
                [],
            )

    def observe_request(self, duration_ms: float) -> None:
        with self._lock:
            if len(self._durations) < 100_000:
                self._durations.append(duration_ms)

    def record_loop(self, name: str, ok: bool, detail: Any = None, next_epoch: float | None = None) -> None:
        self.loops[name] = {"at": time.time(), "ok": ok, "detail": detail, "next": next_epoch}

    def series(self, minutes: int, now: float | None = None) -> list[dict[str, Any]]:
        """The last `minutes` closed minutes, zero-filled where nothing happened."""
        now = time.time() if now is None else now
        end = int(now // 60)
        with self._lock:
            by_minute = {b["minute"]: b for b in self._buckets}
        out = []
        for m in range(end - minutes, end):
            b = by_minute.get(m)
            if b is None:
                b = {k: 0 for k in SERIES_COUNTERS} | {
                    "minute": m,
                    "requests": 0,
                    "p50_ms": None,
                    "p95_ms": None,
                }
            out.append({**b, "epoch": m * 60})
        return out


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 2)
    cuts = statistics.quantiles(sorted(values), n=100, method="inclusive")
    return round(cuts[max(int(q * 100) - 1, 0)], 2)


# ---- helpers --------------------------------------------------------------------------------------
def _today() -> date:
    return datetime.now(tz=UTC).date()


def _day(d: date) -> str:
    return d.isoformat()


# Display unit per meter: (label, how many meter units make one display unit)
DISPLAY_UNITS: dict[str, tuple[str, float]] = {
    "characters": ("per 1K chars", 1000.0),
    "audio_input_seconds": ("per audio min", 60.0),
    "audio_output_seconds": ("per audio min", 60.0),
    "input_tokens": ("per 1M tokens", 1_000_000.0),
    "output_tokens": ("per 1M tokens", 1_000_000.0),
    "cache_read_tokens": ("per 1M tokens", 1_000_000.0),
    "cache_write_tokens": ("per 1M tokens", 1_000_000.0),
    "input_audio_tokens": ("per 1M tokens", 1_000_000.0),
    "output_audio_tokens": ("per 1M tokens", 1_000_000.0),
    "cache_audio_read_tokens": ("per 1M tokens", 1_000_000.0),
    "agent_minutes": ("per min", 1.0),
    "telephony_minutes": ("per min", 1.0),
    "requests": ("per request", 1.0),
}


def _latest_recon(runs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for r in runs:  # newest first
        latest.setdefault((r["project_id"], r["provider"]), r)
    return latest


# ---- views ----------------------------------------------------------------------------------------
def prices_view(pipeline: Pipeline, settings: Settings, project: str | None, days: int) -> dict[str, Any]:
    """Every provider/model/meter that produced a current cost line in the range, with tags."""
    store, pricer = pipeline.store, pipeline.pricer
    today = _today()
    since = _day(today - timedelta(days=days - 1))
    prior_since = _day(today - timedelta(days=2 * days - 1))
    rows = store.admin_price_rows(project, since)
    runs = store.admin_recon_runs(project, _day(today - timedelta(days=14)))
    drifting = {prov for (proj, prov), r in _latest_recon(runs).items() if r["status"] == "drift"}
    freshness_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

    def freshness(provider: str | None, model: str | None) -> dict[str, Any] | None:
        if not provider or not model:
            return None
        key = (provider, model)
        if key not in freshness_cache:
            try:
                freshness_cache[key] = pricer.list_price_freshness(provider, model, today)
            except Exception:
                freshness_cache[key] = None
        return freshness_cache[key]

    priced_total = sum(r["cost_usd"] for r in rows if r["price_source"] in ("voice_prices", "rate_card"))
    out, stale_spend, unpriced_lines, total_lines = [], 0.0, 0, 0
    for r in rows:
        provider, model, meter, source = r["provider"], r["model"], r["meter"], r["price_source"]
        tags: list[dict[str, str]] = []
        fresh = freshness(provider, model) if source in ("voice_prices", "rate_card") else None
        label, size = DISPLAY_UNITS.get(meter, (f"per {meter}", 1.0))
        effective = (
            (r["cost_usd"] / r["quantity"] * size)
            if r["quantity"] and source in ("voice_prices", "rate_card")
            else None
        )
        rate_info: dict[str, Any] | None = None
        is_stale = False
        if source == "voice_prices":
            status = (fresh or {}).get("status")
            if status in STALE_STATUSES:
                tags.append({"kind": "warn", "text": "stale"})
                is_stale = True
            elif status in UNVERIFIED_STATUSES:
                tags.append({"kind": "info", "text": "unverified"})
                is_stale = True
            elif status == "verified":
                tags.append({"kind": "pos", "text": "verified"})
            entries = pricer.rate_cards.entries_for(provider, model, meter)
            if entries and not any(e.active(today) for e in entries):
                tags.append({"kind": "warn", "text": "fell back to list"})
            elif entries:  # a rate card applies today; these lines are from before it took effect
                tags.append({"kind": "info", "text": "before rate card"})
        elif source == "rate_card":
            rate = pricer.rate_cards.find(provider, model, meter, today)
            if rate is not None:
                rate_info = {
                    "unit_price": float(rate.unit_price) if rate.unit_price is not None else None,
                    "unit_size": float(rate.unit_size),
                    "multiplier": float(rate.multiplier) if rate.multiplier is not None else None,
                    "effective_from": rate.effective_from.isoformat() if rate.effective_from else None,
                    "effective_to": rate.effective_to.isoformat() if rate.effective_to else None,
                    "reviewed": rate.reviewed.isoformat() if rate.reviewed else None,
                    "note": rate.note,
                    "model": rate.model,
                }
                tags.append(
                    {
                        "kind": "idle",
                        "text": f"{float(rate.multiplier):g} × list" if rate.multiplier else "rate card",
                    }
                )
                if (
                    rate.multiplier is not None
                    and (fresh or {}).get("status") in STALE_STATUSES + UNVERIFIED_STATUSES
                ):
                    tags.append({"kind": "warn", "text": "stale list"})
                    is_stale = True
                if rate.reviewed is None:
                    tags.append({"kind": "info", "text": "not reviewed"})
                elif (today - rate.reviewed).days > settings.rate_card_review_days:
                    tags.append({"kind": "warn", "text": "stale"})
                    is_stale = True
                if rate.effective_to and 0 <= (rate.effective_to - today).days <= 14:
                    tags.append({"kind": "warn", "text": "expiring"})
            else:
                tags.append({"kind": "idle", "text": "rate card (inactive today)"})
        elif source == "not_billed":
            tags.append({"kind": "idle", "text": "not billed"})
        elif source == "unpriced":
            tags.append({"kind": "neg", "text": "unpriced"})
            unpriced_lines += r["events"]
        if provider in drifting and source in ("voice_prices", "rate_card"):
            tags.append({"kind": "neg", "text": "drifting"})
        if is_stale:
            stale_spend += r["cost_usd"]
        total_lines += r["events"]
        out.append(
            {
                "provider": provider,
                "model": model,
                "component": r["component"],
                "meter": meter,
                "price_source": source,
                "unpriced_reason": r["unpriced_reason"],
                "effective_price": None if effective is None else round(effective, 8),
                "display_unit": label,
                "cost_usd": round(r["cost_usd"], 8),
                "share": round(r["cost_usd"] / priced_total, 6) if priced_total > 0 else 0.0,
                "quantity": r["quantity"],
                "events": r["events"],
                "last_seen_epoch": float(r["last_ts"]) if r["last_ts"] is not None else None,
                "freshness": fresh,
                "rate_card": rate_info,
                "stale": is_stale,
                "tags": tags,
            }
        )
    prior_rows = store.admin_price_rows(project, prior_since)
    prior_total_lines = sum(r["events"] for r in prior_rows) - total_lines
    prior_unpriced = sum(r["events"] for r in prior_rows if r["price_source"] == "unpriced") - unpriced_lines
    last_reprice = store.get_state("last_auto_reprice")
    return {
        "project": project,
        "days": days,
        "rows": out,
        "summary": {
            "priced_spend_usd": round(priced_total, 8),
            "stale_spend_usd": round(stale_spend, 8),
            "stale_share": round(stale_spend / priced_total, 6) if priced_total > 0 else 0.0,
            "rates_in_use": len({(r["provider"], r["model"], r["meter"]) for r in out}),
            "stale_rates": sum(1 for r in out if r["stale"]),
            "unpriced_pairs": len(
                {(r["provider"], r["model"]) for r in out if r["price_source"] == "unpriced"}
            ),
            "unpriced_share": round(unpriced_lines / total_lines, 6) if total_lines else 0.0,
            "prior_unpriced_share": round(prior_unpriced / prior_total_lines, 6)
            if prior_total_lines > 0
            else None,
        },
        "versions": {
            "price_version": pricer.price_version,
            "rate_card_version": pricer.rate_cards.version,
            "rate_card_error": store.get_state("rate_card_error") or None,
            "last_auto_reprice": last_reprice,
        },
    }


def _totals(sessions: list[dict[str, Any]], lines: dict[str, int] | None = None) -> dict[str, Any]:
    """Totals for a set of calls. `lines` (from cost_day) overrides the per-call line counts when given."""
    cost = sum(s["cost_usd"] for s in sessions)
    minutes = sum(s["minutes"] for s in sessions)
    n_lines = lines["lines"] if lines is not None else sum(s["lines"] for s in sessions)
    unpriced = lines["unpriced_lines"] if lines is not None else sum(s["unpriced_lines"] for s in sessions)
    per_call = sorted(s["cost_usd"] for s in sessions)
    p95 = per_call[min(int(round(0.95 * (len(per_call) - 1))), len(per_call) - 1)] if per_call else None
    return {
        "cost_usd": round(cost, 8),
        "calls": len(sessions),
        "minutes": round(minutes, 3),
        "cost_per_minute": round(cost / minutes, 8) if minutes > 0 else None,
        "p95_cost_per_call": None if p95 is None else round(p95, 8),
        "unpriced_share": round(unpriced / n_lines, 6) if n_lines else 0.0,
    }


def days_view(pipeline: Pipeline, project: str, days: int) -> dict[str, Any]:
    """Per-day summary for the Reports view (from call_rollup and cost_day), plus today-so-far against the
    same time yesterday (raw rows, one day only)."""
    store = pipeline.store
    store.flush_cost_days()
    today = _today()
    since = _day(today - timedelta(days=days - 1))
    by_day: dict[str, list[dict[str, Any]]] = {}
    for s in store.rollup_sessions(project, since, None):
        by_day.setdefault(s["day"], []).append(s)
    lines = store.rollup_line_counts_by_day(project, since)
    empty = {"lines": 0, "unpriced_lines": 0}
    highlights = store.admin_highlight_counts(project, since)
    recon: dict[str, list[str]] = {}
    for r in store.recon_runs_since(project, since):
        recon.setdefault(r["day"], []).append(r["status"])
    rows = []
    for i in range(days):
        day = _day(today - timedelta(days=i))
        statuses = recon.get(day, [])
        worst = next(
            (s for s in ("drift", "fetch_failed", "drift_shared_account", "ok") if s in statuses), None
        )
        rows.append(
            {
                "day": day,
                **_totals(by_day.get(day, []), lines.get(day, empty)),
                "highlights": highlights.get(day, 0),
                "recon_status": worst or (statuses[0] if statuses else None),
            }
        )
    now = time.time()
    yesterday = _day(today - timedelta(days=1))
    same_time_yesterday = store.admin_sessions(project, yesterday, yesterday, until_epoch=now - 86_400)
    return {
        "project": project,
        "days": rows,
        "today": {**_totals(by_day.get(_day(today), []), lines.get(_day(today), empty)), "day": _day(today)},
        "yesterday_same_time": _totals(same_time_yesterday),
    }


COST_DIMENSIONS = ("component", "provider", "model", "agent_version", "feature", "region", "env")


def cost_view(
    pipeline: Pipeline, project: str | None, days: int, by: str, filters: dict[str, str]
) -> dict[str, Any]:
    """Cost over a range. Reads the rollups (call_rollup for calls and minutes, cost_day for breakdowns and line
    counts) and falls back to raw rows only for combinations the rollups cannot answer: a tenant filter for
    breakdowns, or a dimension filter for call counts and minutes."""
    store = pipeline.store
    if by not in COST_DIMENSIONS:
        raise ValueError(f"unsupported stack dimension: {by}")
    store.flush_cost_days()
    today = _today()
    since, until = _day(today - timedelta(days=days - 1)), _day(today)
    prior_since, prior_until = _day(today - timedelta(days=2 * days - 1)), _day(today - timedelta(days=days))
    tenant = filters.get("tenant_id")
    dim_filters = {k: v for k, v in filters.items() if k != "tenant_id"}

    if dim_filters:  # call counts under a provider/model/... filter need the raw rows
        sessions = store.admin_sessions(project, since, until, filters)
        prior = store.admin_sessions(project, prior_since, prior_until, filters)
    else:
        sessions = store.rollup_sessions(project, since, until, tenant)
        prior = store.rollup_sessions(project, prior_since, prior_until, tenant)
    if tenant:  # cost_day has no tenant column
        daily = store.admin_cost_by_day(project, since, until, by, filters)
        by_provider = store.admin_cost_by(project, since, until, "provider", filters)
        by_model = store.admin_cost_by(project, since, until, "model", filters)
        lines = prior_lines = None if dim_filters else {"lines": 0, "unpriced_lines": 0}
        if not dim_filters:  # rollup sessions carry no line counts: take them from the raw rows
            raw = store.admin_sessions(project, since, until, filters)
            raw_prior = store.admin_sessions(project, prior_since, prior_until, filters)
            lines = {
                "lines": sum(s["lines"] for s in raw),
                "unpriced_lines": sum(s["unpriced_lines"] for s in raw),
            }
            prior_lines = {
                "lines": sum(s["lines"] for s in raw_prior),
                "unpriced_lines": sum(s["unpriced_lines"] for s in raw_prior),
            }
    else:
        daily = store.rollup_cost_by_day(project, since, until, by, dim_filters)
        by_provider = store.rollup_cost_by(project, since, until, "provider", dim_filters)
        by_model = store.rollup_cost_by(project, since, until, "model", dim_filters)
        lines = store.rollup_line_counts(project, since, until, dim_filters)
        prior_lines = store.rollup_line_counts(project, prior_since, prior_until, dim_filters)

    tenants: dict[str, dict[str, Any]] = {}
    for s in sessions:
        t = tenants.setdefault(
            s["tenant_id"] or "(none)",
            {"tenant_id": s["tenant_id"], "cost_usd": 0.0, "calls": 0, "minutes": 0.0},
        )
        t["cost_usd"] += s["cost_usd"]
        t["calls"] += 1
        t["minutes"] += s["minutes"]
    prior_tenant: dict[str | None, float] = {}
    for s in prior:
        prior_tenant[s["tenant_id"]] = prior_tenant.get(s["tenant_id"], 0.0) + s["cost_usd"]
    top = sorted(tenants.values(), key=lambda t: -t["cost_usd"])[:10]
    for t in top:
        before = prior_tenant.get(t["tenant_id"])
        t["cost_per_minute"] = round(t["cost_usd"] / t["minutes"], 8) if t["minutes"] > 0 else None
        t["change"] = round((t["cost_usd"] - before) / before, 6) if before else None
        t["cost_usd"] = round(t["cost_usd"], 8)

    recon_since = _day(today - timedelta(days=14))
    recon = [
        {
            "day": r["day"],
            "project": r["project_id"],
            "provider": r["provider"],
            "status": r["status"],
            "drift_pct": r.get("drift_pct"),
            "unit_drift": (r.get("detail") or {}).get("unit_drift") or {},
        }
        for r in store.admin_recon_runs(project, recon_since)
    ]
    return {
        "project": project,
        "days": days,
        "by": by,
        "filters": filters,
        "range": {"since": since, "until": until},
        "read_from": {
            "calls": "raw" if dim_filters else "call_rollup",
            "breakdowns": "raw" if tenant else "cost_day",
        },
        "totals": _totals(sessions, lines),
        "prior": _totals(prior, prior_lines),
        "daily": daily,
        "by_provider": by_provider,
        "by_model": by_model,
        "tenants": top,
        "recon": recon,
        "options": {
            c: store.rollup_distinct(project, since, c)
            for c in ("provider", "component", "model", "feature", "agent_version", "region", "env")
        },
    }


def health_view(
    pipeline: Pipeline, settings: Settings, monitor: HealthMonitor, minutes: int = 180
) -> dict[str, Any]:
    from .reconcile import load_accounts

    store = pipeline.store
    now = time.time()
    monitor.sample(dict(pipeline.counters), now)
    started = time.perf_counter()
    db_ok = store.ping()
    ping_ms = round((time.perf_counter() - started) * 1000.0, 2)
    spool_files = pipeline.spool.pending()
    spool_bytes = pipeline.spool.size_bytes()
    series = monitor.series(minutes, now)

    try:
        accounts = load_accounts(os.environ.get("VOICETOLL_RECON_CONFIG"))
        accounts_error = None
    except Exception as exc:
        accounts, accounts_error = [], f"{type(exc).__name__}: {exc}"
    latest = _latest_recon(store.admin_recon_runs(None, _day(_today() - timedelta(days=14))))
    connectors = []
    from .reconcile import DEFAULT_FETCHERS

    for a in accounts:
        run = latest.get((a.project, a.provider))
        connectors.append(
            {
                "project": a.project,
                "provider": a.provider,
                "scope": "dedicated" if a.dedicated else "shared",
                "key_set": bool(a.api_key()),
                "key_from": "agent key" if a.uses_agent_key() else ("reconciliation key" if a.api_key() else None),
                "has_connector": a.provider in DEFAULT_FETCHERS,
                "last_status": run["status"] if run else None,
                "last_day": run["day"] if run else None,
                "drift_days": int(((run or {}).get("detail") or {}).get("consecutive_drift_days", 0)),
            }
        )

    replay = monitor.loops.get("replay")
    spool_state = "healthy"
    if spool_files:
        stuck = replay is not None and not replay["ok"]
        spool_state = "failing" if stuck and spool_bytes > 0.8 * settings.spool_max_bytes else "degraded"
    jobs = monitor.loops.get("jobs")
    pricing = monitor.loops.get("pricing")
    checks = [
        {
            "name": "Collector",
            "state": "healthy",
            "note": f"up since {datetime.fromtimestamp(monitor.started_epoch, tz=UTC):%Y-%m-%d %H:%M} UTC",
        },
        {
            "name": "Database",
            "state": "healthy" if db_ok else "failing",
            "note": f"{store.dialect} · ping {ping_ms} ms",
        },
        {
            "name": "Spool",
            "state": spool_state,
            "note": f"{len(spool_files)} file(s) · {spool_bytes / 1e6:.1f} of {settings.spool_max_bytes / 1e6:,.0f} MB"
            + ("" if not replay or replay["ok"] else " · last replay failed"),
        },
        {
            "name": "Jobs loop",
            "state": "not configured" if jobs is None else ("healthy" if jobs["ok"] else "degraded"),
            "note": "not run yet" if jobs is None else f"last run {'ok' if jobs['ok'] else 'failed'}",
        },
        {
            "name": "Pricing check",
            "state": "not configured" if pricing is None else ("healthy" if pricing["ok"] else "degraded"),
            "note": "not run yet" if pricing is None else f"last run {'ok' if pricing['ok'] else 'failed'}",
        },
        {
            "name": "Config",
            "state": "degraded" if (store.get_state("rate_card_error") or accounts_error) else "healthy",
            "note": f"rate cards {'error' if store.get_state('rate_card_error') else 'ok'} · {len(accounts)} recon account(s)",
        },
    ]
    clients = store.client_stats_rows(now - 7 * 86_400)
    active = [c for c in clients if c["reported_epoch"] >= now - 3600]
    dropping = [c for c in active if int(c["dropped"] or 0) > 0]
    # sent (client, acknowledged batches) vs received_before (collector, same batches): below means lost events
    missing = [c for c in active if int(c.get("sent") or 0) > int(c.get("received_before") or 0)]
    checks.append(
        {
            "name": "Clients",
            "state": "not configured" if not clients else ("degraded" if dropping or missing else "healthy"),
            "note": "no client has reported yet (clients send counters with each batch)"
            if not clients
            else f"{len(active)} active in the last hour · {len(dropping)} dropping events"
            + (f" · {len(missing)} sent more than arrived" if missing else ""),
        }
    )
    rejected = pipeline.spool.rejected()
    if rejected:
        for check in checks:
            if check["name"] == "Spool":
                check["state"] = "degraded" if check["state"] == "healthy" else check["state"]
                check["note"] += f" · {len(rejected)} rejected file(s) set aside"
    counters = dict(pipeline.counters)
    last_hour = monitor.series(60, now)
    return {
        "now": now,
        "replica": monitor.replica,
        "started_epoch": monitor.started_epoch,
        "checks": checks,
        "loops": monitor.loops,
        "counters": counters,
        "last_hour": {k: sum(b[k] for b in last_hour) for k in SERIES_COUNTERS},
        "series": series,
        "sources": store.admin_sources(now - 7 * 86_400, now - 3600),
        "connectors": connectors,
        "connectors_error": accounts_error,
        "warnings": sorted(recent_warnings().records, key=lambda r: -r["epoch"]),
        "versions": {
            "price_version": pipeline.pricer.price_version,
            "rate_card_version": pipeline.pricer.rate_cards.version,
        },
        "spool": {
            "files": len(spool_files),
            "bytes": spool_bytes,
            "max_bytes": settings.spool_max_bytes,
            "rejected_files": [p.name for p in rejected],
        },
        "clients": [
            {
                "project": c["project_id"],
                "client_id": c["client_id"],
                "sdk_version": c["sdk_version"],
                "source": c["source"],
                "dropped": int(c["dropped"] or 0),
                "errors": int(c["errors"] or 0),
                "sent": int(c["sent"] or 0),
                "received": int(c.get("received") or 0),
                "received_before": int(c.get("received_before") or 0),
                "buffer_len": int(c["buffer_len"] or 0),
                "buffer_max": int(c["buffer_max"] or 0),
                "started_epoch": c["started_epoch"],
                "reported_epoch": c["reported_epoch"],
            }
            for c in clients
        ],
    }
