"""Background jobs: reconciliation, price freshness, then highlights (M3/M4)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .pipeline import Pipeline
    from .settings import Settings

log = logging.getLogger("voicetoll.collector.jobs")


def run_scheduled_jobs(pipeline: Pipeline, settings: Settings, *, reconcile: bool = True) -> dict[str, Any]:
    from .highlights import HighlightRules, compute_highlights
    from .reconcile import run_reconciliation
    from .upkeep import check_price_freshness

    result: dict[str, Any] = {}

    # 0. First run after upgrading: fill the cost_day rollup from existing history (once)
    if pipeline.store.cost_day_needs_backfill():
        result["cost_day_backfilled_days"] = pipeline.store.rebuild_cost_days()

    # 1. Reconcile yesterday, once per configured provider account (config/reconcile.yaml). Provider
    #    bills are account-wide, so each account is compared once, against the project it maps to.
    if reconcile:
        yesterday = (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        recon_rows = run_reconciliation(
            pipeline.store, None, yesterday, drift_threshold=settings.recon_drift_threshold
        )
        recon: dict[str, list[str]] = {}
        for r in recon_rows:
            recon.setdefault(r["project_id"], []).append(f"{r['provider']}:{r['status']}")
        result["recon"] = recon

    # 2. Price freshness per project, then highlights (which read freshness and reconciliation results)
    since = (datetime.now(tz=UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    rules = HighlightRules.from_settings(settings)
    highlight_counts, stale_counts = {}, {}
    for project in pipeline.store.projects_with_events(since):
        fresh = check_price_freshness(pipeline.store, pipeline.pricer, project)
        stale_counts[project] = sum(1 for r in fresh if r["status"] in ("stale", "seed", "imported"))
        highlight_counts[project] = len(compute_highlights(pipeline.store, project, days=7, rules=rules))
    result.update({"highlights": highlight_counts, "stale_prices": stale_counts})
    log.info("jobs complete: %s", result)
    return result
