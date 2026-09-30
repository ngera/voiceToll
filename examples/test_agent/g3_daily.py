"""G3 daily check: voiceToll's estimate per provider vs what the providers report, logged to a CSV.

Run once a day for the G3 week, after the day is over (UTC):

    uv run python examples/test_agent/g3_daily.py                 # yesterday, project "demo"
    uv run python examples/test_agent/g3_daily.py --day 2026-10-02

It reads the collector's database directly (VOICETOLL_DB_URL), runs reconciliation for every account in
VOICETOLL_RECON_CONFIG, prints a table and appends rows to examples/test_agent/g3_log.csv. Where a
provider API gives no number (or you have no usage key), copy the figure from the provider's dashboard
into the `dashboard_*` columns of the CSV by hand.
"""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from voicetoll_collector.reconcile import run_reconciliation
from voicetoll_collector.settings import Settings
from voicetoll_collector.store import Store

LOG = Path(__file__).with_name("g3_log.csv")
COLUMNS = [
    "day", "project", "provider", "est_usd", "est_units", "reported_usd", "reported_units",
    "usd_drift", "unit_drift", "status", "dashboard_usd", "dashboard_units", "notes",
]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", default=(datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d"))
    parser.add_argument("--project", default="demo")
    args = parser.parse_args()

    settings = Settings.from_env()
    store = Store(settings.db_url)
    recon = {r["provider"]: r for r in run_reconciliation(store, args.project, args.day,
                                                          drift_threshold=settings.recon_drift_threshold)}
    providers = [r["provider"] for r in store._query(
        "SELECT DISTINCT provider FROM usage_event WHERE project_id = ? AND day = ? AND provider IS NOT NULL",
        (args.project, args.day),
    )]

    rows = []
    for provider in sorted(set(providers) | set(recon)):
        r = recon.get(provider, {})
        detail = r.get("detail") or {}
        rows.append({
            "day": args.day,
            "project": args.project,
            "provider": provider,
            "est_usd": round(store.estimated_provider_day(args.project, provider, args.day), 6),
            "est_units": json.dumps({k: round(v, 2) for k, v in
                                     store.estimated_provider_units(args.project, provider, args.day).items()}),
            "reported_usd": r.get("reported_usd"),
            "reported_units": json.dumps(detail.get("reported_units") or {}),
            "usd_drift": r.get("drift_pct"),
            "unit_drift": json.dumps(detail.get("unit_drift") or {}),
            "status": r.get("status", "not_configured"),
            "dashboard_usd": "",
            "dashboard_units": "",
            "notes": "",
        })

    new_file = not LOG.exists()
    with LOG.open("a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerows(rows)

    print(f"G3 check for {args.project} on {args.day}")
    print(f"{'provider':<12} {'est $':>10} {'reported $':>11} {'$ drift':>8}  {'unit drift':<28} status")
    for row in rows:
        rep = "" if row["reported_usd"] is None else f"{row['reported_usd']:.4f}"
        drift = "" if row["usd_drift"] is None else f"{row['usd_drift'] * 100:.1f}%"
        print(f"{row['provider']:<12} {row['est_usd']:>10.4f} {rep:>11} {drift:>8}  {row['unit_drift']:<28} "
              f"{row['status']}")
    print(f"\nAppended {len(rows)} rows to {LOG}. Fill dashboard_* by hand where status is not ok.")


if __name__ == "__main__":
    main()
