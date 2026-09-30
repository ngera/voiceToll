"""Rule-based highlights over rollups / raw events (M3)."""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .store import Store, _num, _pct


@dataclass
class HighlightRules:
    """Thresholds for the rules; the collector fills these from Settings."""

    v2v_budget_ms: float = 1200.0
    release_change_threshold: float = 0.15
    min_calls: int = 3
    min_turns: int = 20

    @classmethod
    def from_settings(cls, settings: Any) -> HighlightRules:
        return cls(
            v2v_budget_ms=settings.v2v_budget_ms,
            release_change_threshold=settings.release_change_threshold,
            min_calls=settings.highlight_min_calls,
            min_turns=settings.highlight_min_turns,
        )


STAGE_LABELS = {"eou": "Turn detection (end of utterance)", "llm": "LLM", "tts": "TTS"}


def turn_latencies(store: Store, project: str, since_day: str) -> list[dict[str, Any]]:
    """Voice-to-voice per user turn: end-of-utterance delay + LLM time to first token + TTS time to first
    audio. Events are paired in time order within each call (a turn-timing event opens a turn; the next
    LLM and TTS events with timings close it), which does not depend on framework turn ids."""
    rows = store._query(
        "SELECT session_id, ts_epoch, component, provider, model, agent_version, eou_delay_ms, ttft_ms, ttfb_ms "
        "FROM usage_event WHERE project_id = ? AND day >= ? AND component IN ('turn', 'llm', 'tts', 's2s') "
        "ORDER BY session_id, ts_epoch",
        (project, since_day),
    )
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    session = None

    def close(turn: dict[str, Any] | None) -> None:
        if turn is None or turn.get("eou") is None:
            return
        if turn.get("s2s") is not None:
            turn["v2v"] = turn["eou"] + turn["s2s"]
            turns.append(turn)
        elif turn.get("llm") is not None and turn.get("tts") is not None:
            turn["v2v"] = turn["eou"] + turn["llm"] + turn["tts"]
            turns.append(turn)

    for row in rows:
        if row["session_id"] != session:
            close(current)
            current, session = None, row["session_id"]
        comp = row["component"]
        if comp == "turn":
            close(current)
            eou = row["eou_delay_ms"]
            current = {
                "session_id": session,
                "ts": float(row["ts_epoch"]),
                "agent_version": row["agent_version"],
                "eou": float(eou) if eou is not None else None,
                "llm": None,
                "tts": None,
                "s2s": None,
            }
        elif current is not None:
            if comp == "llm" and current["llm"] is None and row["ttft_ms"] is not None:
                current["llm"] = float(row["ttft_ms"])
                current["llm_model"] = f"{row['provider'] or '?'} {row['model'] or ''}".strip()
            elif comp == "s2s" and current["s2s"] is None and row["ttft_ms"] is not None:
                current["s2s"] = float(row["ttft_ms"])
            elif comp == "tts" and current["tts"] is None and row["ttfb_ms"] is not None:
                current["tts"] = float(row["ttfb_ms"])
                current["tts_model"] = f"{row['provider'] or '?'} {row['model'] or ''}".strip()
    close(current)
    return turns


def _slow_component(project: str, turns: list[dict[str, Any]], rules: HighlightRules) -> dict[str, Any] | None:
    if len(turns) < rules.min_turns:
        return None
    over = [t for t in turns if t["v2v"] > rules.v2v_budget_ms and t.get("s2s") is None]
    if len(over) < 5 or len(over) / len(turns) < 0.10:
        return None
    culprits: dict[str, int] = {}
    for t in over:
        stage = max(("eou", "llm", "tts"), key=lambda k: t[k])
        culprits[stage] = culprits.get(stage, 0) + 1
    stage, count = max(culprits.items(), key=lambda kv: kv[1])
    if count / len(over) < 0.5:
        return None
    values = [t[stage] for t in turns]
    label = STAGE_LABELS[stage]
    who = ""
    if stage in ("llm", "tts"):
        names = [t.get(f"{stage}_model") for t in over if t.get(f"{stage}_model")]
        if names:
            who = f" ({max(set(names), key=names.count)})"
    budget_s = rules.v2v_budget_ms / 1000
    return {
        "id": _hid("slow_component", project, stage),
        "rule_id": "slow_component",
        "title": f"{label}{who} is the slowest step",
        "detail": (
            f"{len(over)} of {len(turns)} turns ({len(over) / len(turns):.0%}) took longer than {budget_s:.1f} s "
            f"from the caller finishing to the agent's first audio; {label.split(' (')[0]} was the biggest part "
            f"in {count} of them (p50 {_pct(values, 50):.0f} ms, p95 {_pct(values, 95):.0f} ms)."
        ),
        "dollars_at_stake": 0.0,
        "evidence": {
            "stage": stage,
            "turns": len(turns),
            "turns_over_budget": len(over),
            "budget_ms": rules.v2v_budget_ms,
            "culprit_counts": culprits,
            "p50_ms": _pct(values, 50),
            "p95_ms": _pct(values, 95),
            "p95_v2v_ms": _pct([t["v2v"] for t in turns], 95),
        },
        "status": "open",
    }


def _version_calls(store: Store, project: str, since_day: str) -> list[dict[str, Any]]:
    rows = store._query(
        "SELECT session_id, MIN(agent_version) AS agent_version, MIN(ts_epoch) AS started, MAX(ts_epoch) AS ended "
        "FROM usage_event WHERE project_id = ? AND day >= ? AND agent_version IS NOT NULL GROUP BY session_id",
        (project, since_day),
    )
    costs = store._query(
        "SELECT e.session_id AS session_id, COALESCE(SUM(c.amount_usd), 0) AS cost_usd FROM usage_event e "
        "JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
        "WHERE e.project_id = ? AND e.day >= ? GROUP BY e.session_id",
        (project, since_day),
    )
    by_session = {r["session_id"]: _num(r["cost_usd"]) for r in costs}
    return [
        {
            "session_id": r["session_id"],
            "agent_version": r["agent_version"],
            "started": float(r["started"]),
            "minutes": max((float(r["ended"]) - float(r["started"])) / 60.0, 0.0),
            "cost": by_session.get(r["session_id"], 0.0),
        }
        for r in rows
    ]


def _release_change(
    store: Store, project: str, days: int, turns: list[dict[str, Any]], rules: HighlightRules
) -> dict[str, Any] | None:
    since = (datetime.now(tz=UTC) - timedelta(days=days + 7)).strftime("%Y-%m-%d")
    calls = _version_calls(store, project, since)
    first_seen: dict[str, float] = {}
    for c in calls:
        v = c["agent_version"]
        first_seen[v] = min(first_seen.get(v, c["started"]), c["started"])
    if len(first_seen) < 2:
        return None
    ordered = sorted(first_seen, key=first_seen.get)
    new, prev = ordered[-1], ordered[-2]
    cutoff = first_seen[new]
    if cutoff < datetime.now(tz=UTC).timestamp() - days * 86400:
        return None  # the release is older than the window; nothing new to say
    after = [c for c in calls if c["agent_version"] == new]
    before = [c for c in calls if c["agent_version"] == prev and cutoff - 7 * 86400 <= c["started"] < cutoff]
    if len(after) < rules.min_calls or len(before) < rules.min_calls:
        return None
    min_new = sum(c["minutes"] for c in after)
    min_old = sum(c["minutes"] for c in before)
    if min_new <= 0 or min_old <= 0:
        return None
    cpm_new = sum(c["cost"] for c in after) / min_new
    cpm_old = sum(c["cost"] for c in before) / min_old
    cpm_change = (cpm_new - cpm_old) / cpm_old if cpm_old > 0 else None

    v2v_new = [t["v2v"] for t in turns if t.get("agent_version") == new]
    v2v_old = [t["v2v"] for t in turns if t.get("agent_version") == prev and t["ts"] >= cutoff - 7 * 86400]
    p95_new = p95_old = p95_change = None
    need = max(rules.min_turns // 2, 5)
    if len(v2v_new) >= need and len(v2v_old) >= need:
        p95_new, p95_old = _pct(v2v_new, 95), _pct(v2v_old, 95)
        if p95_old:
            p95_change = (p95_new - p95_old) / p95_old

    t = rules.release_change_threshold
    cost_moved = cpm_change is not None and abs(cpm_change) >= t
    latency_moved = p95_change is not None and abs(p95_change) >= t
    if not (cost_moved or latency_moved):
        return None
    parts = []
    if cost_moved:
        parts.append(f"cost per minute {'up' if cpm_change > 0 else 'down'} {abs(cpm_change):.0%}")
    if latency_moved:
        parts.append(f"p95 voice-to-voice {'up' if p95_change > 0 else 'down'} {abs(p95_change):.0%}")
    since_label = datetime.fromtimestamp(cutoff, tz=UTC).strftime("%Y-%m-%d")
    detail = (
        f"Version {new} (since {since_label}, {len(after)} calls): ${cpm_new:.4f}/min vs ${cpm_old:.4f}/min on "
        f"{prev} ({len(before)} calls in the 7 days before)"
    )
    if p95_new is not None:
        detail += f"; p95 voice-to-voice {p95_new / 1000:.2f} s vs {p95_old / 1000:.2f} s"
    extra = (cpm_new - cpm_old) * min_new if cpm_change and cpm_change > 0 else 0.0
    return {
        "id": _hid("release_change", project, f"{prev}->{new}"),
        "rule_id": "release_change",
        "title": f"After release {new}: " + " and ".join(parts),
        "detail": detail + ".",
        "dollars_at_stake": round(extra, 4),
        "evidence": {
            "new_version": new,
            "previous_version": prev,
            "released_epoch": cutoff,
            "calls_new": len(after),
            "calls_previous": len(before),
            "cost_per_minute_new": cpm_new,
            "cost_per_minute_previous": cpm_old,
            "cost_change": cpm_change,
            "p95_v2v_ms_new": p95_new,
            "p95_v2v_ms_previous": p95_old,
            "p95_change": p95_change,
        },
        "status": "open",
    }


def _stale_prices(store: Store, project: str) -> list[dict[str, Any]]:
    items = []
    for row in store.price_freshness_rows(project):
        if row["status"] not in ("stale", "seed", "imported") or _num(row.get("list_spend_usd")) <= 0:
            continue
        if row["status"] == "stale":
            why = f"last verified {row['last_verified']} ({row['age_days']} days ago)"
        else:
            why = "never verified by a person" + (" (imported from another catalog)" if row["status"] == "imported" else "")
        items.append(
            {
                "id": _hid("stale_price", project, f"{row['provider']}|{row['model']}"),
                "rule_id": "stale_price",
                "title": f"Price for {row['provider']} {row['model']} is {row['status']}",
                "detail": (
                    f"voice-prices rate {why}; ${_num(row['list_spend_usd']):.4f} of the last 7 days' estimate "
                    "uses it. Add a rate-card entry with your contracted rate, or update voice-prices."
                ),
                "dollars_at_stake": round(_num(row["list_spend_usd"]), 4),
                "evidence": dict(row),
                "status": "open",
            }
        )
    return items


def _hid(rule: str, project: str, key: str) -> str:
    return hashlib.sha256(f"{rule}|{project}|{key}".encode()).hexdigest()[:20]


def compute_highlights(
    store: Store, project: str, days: int = 7, rules: HighlightRules | None = None
) -> list[dict[str, Any]]:
    rules = rules or HighlightRules()
    since = (datetime.now(tz=UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    items: list[dict[str, Any]] = []

    tenant_rows = store.tenant_day_rows(project, since)
    if not tenant_rows:
        # Fall back to query-time top tenants for each day in the window
        for offset in range(days):
            day = (datetime.now(tz=UTC) - timedelta(days=offset)).strftime("%Y-%m-%d")
            for row in store.top_tenants(project, day, limit=50):
                tenant_rows.append(
                    {
                        "tenant_id": row["tenant_id"],
                        "day": day,
                        "calls": row["calls"],
                        "minutes": 0.0,
                        "cost_usd": row["cost_usd"],
                    }
                )

    # Heavy tenants: cost/min above 3x median when minutes known; else top by weekly cost
    by_tenant: dict[str, dict[str, float]] = {}
    for row in tenant_rows:
        tid = row.get("tenant_id") or "unknown"
        agg = by_tenant.setdefault(tid, {"cost": 0.0, "minutes": 0.0, "calls": 0.0})
        agg["cost"] += _num(row.get("cost_usd"))
        agg["minutes"] += float(row.get("minutes") or 0)
        agg["calls"] += float(row.get("calls") or 0)
    cpms = []
    for _tid, agg in by_tenant.items():
        if agg["minutes"] > 0:
            cpms.append(agg["cost"] / agg["minutes"])
    median_cpm = statistics.median(cpms) if cpms else None
    ranked = sorted(by_tenant.items(), key=lambda kv: kv[1]["cost"], reverse=True)
    for tid, agg in ranked[:5]:
        cpm = (agg["cost"] / agg["minutes"]) if agg["minutes"] > 0 else None
        heavy = median_cpm is not None and cpm is not None and cpm > 3 * median_cpm
        top = ranked and tid == ranked[0][0] and agg["cost"] > 0
        if heavy or top:
            items.append(
                {
                    "id": _hid("heavy_tenant", project, tid),
                    "rule_id": "heavy_tenant",
                    "title": f"Heavy tenant {tid[:12]}",
                    "detail": f"Weekly cost ${agg['cost']:.4f}"
                    + (f", ${cpm:.4f}/min (median ${median_cpm:.4f})" if cpm and median_cpm else ""),
                    "dollars_at_stake": round(agg["cost"], 4),
                    "evidence": {"tenant_id": tid, "cost_usd": agg["cost"], "minutes": agg["minutes"]},
                    "status": "open",
                }
            )

    # Long-session cost growth: calls over 20 minutes
    for row in store.call_rollup_rows(project, since):
        minutes = float(row.get("minutes") or 0)
        if minutes >= 20:
            items.append(
                {
                    "id": _hid("long_session", project, row["session_id"]),
                    "rule_id": "long_session",
                    "title": f"Long session {row['session_id'][:16]}",
                    "detail": f"{minutes:.1f} minutes, ${_num(row.get('cost_usd')):.4f}",
                    "dollars_at_stake": round(_num(row.get("cost_usd")), 4),
                    "evidence": {"session_id": row["session_id"], "minutes": minutes},
                    "status": "open",
                }
            )

    # Wasted speech: cancelled TTS share from raw events
    cancelled = store._query(
        "SELECT e.session_id AS session_id, "
        "SUM(CASE WHEN e.cancelled = 1 THEN 1 ELSE 0 END) AS cancelled_n, COUNT(*) AS n "
        "FROM usage_event e WHERE e.project_id = ? AND e.day >= ? AND e.component = 'tts' "
        "GROUP BY e.session_id",
        (project, since),
    )
    for row in cancelled:
        n = int(row["n"] or 0)
        c = int(row["cancelled_n"] or 0)
        if n >= 5 and c / n >= 0.10:
            items.append(
                {
                    "id": _hid("wasted_speech", project, row["session_id"]),
                    "rule_id": "wasted_speech",
                    "title": "Wasted TTS (interrupted)",
                    "detail": f"{c}/{n} TTS events cancelled on session {row['session_id'][:16]}",
                    "dollars_at_stake": 0.0,
                    "evidence": {"session_id": row["session_id"], "cancelled": c, "tts_events": n},
                    "status": "open",
                }
            )

    # Unpriced share for the window
    cov = store.coverage_report(project, today)
    if cov.get("unpriced_share", 0) >= 0.05 and cov.get("cost_lines", 0) > 0:
        items.append(
            {
                "id": _hid("unpriced", project, today),
                "rule_id": "unpriced_share",
                "title": "Unpriced usage share elevated",
                "detail": f"{cov['unpriced_share'] * 100:.1f}% of cost lines unpriced on {today}",
                "dollars_at_stake": 0.0,
                "evidence": cov,
                "status": "open",
            }
        )

    # Reconciliation drift two or more days running on a dedicated account. Only a provider's most recent
    # run counts: once a later day reconciles cleanly, the older alert is no longer news.
    seen_providers: set[str] = set()
    for run in store.recon_runs_since(project, since):  # newest day first
        if run["provider"] in seen_providers:
            continue
        seen_providers.add(run["provider"])
        detail = run.get("detail") or {}
        if not detail.get("alert"):
            continue
        gaps = []
        if run.get("drift_pct") is not None:
            gaps.append(f"dollars {_num(run['drift_pct']) * 100:.1f}% apart")
        for unit, value in (detail.get("unit_drift") or {}).items():
            gaps.append(f"{unit.replace('_', ' ')} {_num(value) * 100:.1f}% apart")
        items.append(
            {
                "id": _hid("recon_drift", project, f"{run['provider']}|{run['day']}"),
                "rule_id": "recon_drift",
                "title": f"Estimate drifts from {run['provider']} usage",
                "detail": (
                    f"{detail.get('consecutive_drift_days')} days running, latest {run['day']}: "
                    + ("; ".join(gaps) if gaps else "no comparable numbers")
                ),
                "dollars_at_stake": abs(_num(run.get("estimated_usd")) - _num(run.get("reported_usd")))
                if run.get("reported_usd") is not None
                else 0.0,
                "evidence": {"provider": run["provider"], "day": run["day"], "status": run["status"], **detail},
                "status": "open",
            }
        )

    # Change after a release, slowest component, stale prices
    wide_since = (datetime.now(tz=UTC) - timedelta(days=days + 7)).strftime("%Y-%m-%d")
    all_turns = turn_latencies(store, project, wide_since)  # includes the baseline week before a release
    since_epoch = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    recent_turns = [t for t in all_turns if t["ts"] >= since_epoch]
    for maker in (
        lambda: _release_change(store, project, days, all_turns, rules),
        lambda: _slow_component(project, recent_turns, rules),
    ):
        item = maker()
        if item:
            items.append(item)
    items.extend(_stale_prices(store, project))

    items.sort(key=lambda x: x.get("dollars_at_stake", 0), reverse=True)
    store.save_highlights(project, today, items)
    return items
