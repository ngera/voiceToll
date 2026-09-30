"""Command line: `voicetoll-collector serve | price | replay-spool | init-db | reprice | reconcile | recon-capture |
audit-call | doctor | prices | import-vapi`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta

from .settings import Settings


def _drop_client_disconnects(record: logging.LogRecord) -> bool:
    """Hide a harmless Windows asyncio error: when a browser or client closes its connection first (a closed tab,
    the admin page's auto-refresh), the Proactor event loop logs ConnectionResetError [WinError 10054] while
    tidying up. Nothing failed, so it should not look like an error."""
    exc = record.exc_info[1] if record.exc_info else None
    return not (isinstance(exc, ConnectionResetError) and "_call_connection_lost" in record.getMessage())


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncio").addFilter(_drop_client_disconnects)
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_price(args: argparse.Namespace) -> int:
    from .pricing import RateCards, price_units

    units: dict[str, float] = {}
    for item in args.unit:
        name, _, value = item.partition("=")
        units[name] = float(value)
    cards = RateCards.load(args.rate_cards or Settings.from_env().rate_cards_path)
    lines = price_units(args.provider, args.model, units, rate_cards=cards)
    total = sum(line.amount_usd for line in lines)
    for line in lines:
        extra = f" ({line.unpriced_reason})" if line.unpriced_reason else ""
        print(
            f"{line.meter:<22} {line.quantity:>12}  ${line.amount_usd:.8f}  {line.price_source}{extra}  "
            f"freshness={line.freshness}"
        )
    print(f"{'total':<22} {'':>12}  ${total:.8f}")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from .app import build_pipeline

    pipeline = build_pipeline(Settings.from_env())
    print(json.dumps({"replayed": pipeline.replay_spool()}))
    return 0


def _cmd_init_db(args: argparse.Namespace) -> int:
    from .store import Store

    Store(Settings.from_env().db_url)
    print("schema ready")
    return 0


def _cmd_reprice(args: argparse.Namespace) -> int:
    from .app import build_pipeline
    from .reprice import reprice

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    result = reprice(
        pipeline.store,
        pipeline.pricer,
        args.project,
        provider=args.provider,
        model=args.model,
        since_day=args.since,
    )
    print(json.dumps(result))
    return 0


def _cmd_rebuild_rollups(args: argparse.Namespace) -> int:
    from .store import Store

    store = Store(Settings.from_env().db_url)
    days = store.rebuild_cost_days(project=args.project, since_day=args.since)
    print(json.dumps({"cost_day_days_rebuilt": days}))
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from .app import build_pipeline
    from .reconcile import run_reconciliation

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    day = args.day or (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    rows = run_reconciliation(
        pipeline.store, args.project, day, drift_threshold=settings.recon_drift_threshold
    )
    if not rows:
        print(
            "no reconciliation accounts configured: set VOICETOLL_RECON_CONFIG (see config/reconcile.example.yaml)"
        )
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_audit_call(args: argparse.Namespace) -> int:
    from .audit import audit_call, format_audit
    from .store import Store

    settings = Settings.from_env()
    store = Store(settings.db_url)
    project = args.project
    if not project:  # find the call's project
        found = store._query(
            "SELECT DISTINCT project_id FROM usage_event WHERE session_id = ?", (args.session,)
        )
        if len(found) > 1:
            names = ", ".join(r["project_id"] for r in found)
            print(f"call {args.session!r} exists in several projects ({names}); pass --project")
            return 1
        project = found[0]["project_id"] if found else "default"
    result = audit_call(store, project, args.session, pad_before=args.pad_before, pad_after=args.pad_after)
    if result is None:
        print(f"no events for call {args.session!r} in project {project!r}")
        return 1
    print(json.dumps(result, indent=2) if args.json else format_audit(result))
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from .app import build_pipeline
    from .doctor import format_report, run_doctor, write_config

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    report = run_doctor(pipeline.store, pipeline.pricer, project=args.project, offline=args.offline)
    print(format_report(report))
    if args.write:
        path, written = write_config(report)
        if written:
            print(f"Saved {', '.join(written)} to {path}.")
            if not report["config_env_set"]:
                print(f"Add this line to .env so the collector reads it:  VOICETOLL_RECON_CONFIG={path}")
            print("Restart the collector to pick it up.")
        else:
            print("Nothing to save: no provider passed its checks.")
    elif any(r.usable for r in report["providers"]):
        print("Run again with --write to save these settings to the reconciliation config.")
    failing = any(c.state == "fail" for c in report["setup"]) or any(
        c.state == "fail" for r in report["providers"] for c in r.checks
    )
    return 1 if failing else 0


def _cmd_recon_capture(args: argparse.Namespace) -> int:
    import os

    from .reconcile import capture_fixtures, load_accounts

    accounts = load_accounts(os.environ.get("VOICETOLL_RECON_CONFIG"))
    if not accounts:
        print(
            "no reconciliation accounts configured: set VOICETOLL_RECON_CONFIG (see config/reconcile.example.yaml)"
        )
        return 1
    day = args.day or (datetime.now(tz=UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    results = capture_fixtures(accounts, day, args.out)
    for r in results:
        numbers = f"usd={r.get('usd')} units={r.get('units')}" if r["status"] == "saved" else ""
        print(f"{r['provider']:<12} {r['status']:<32} {numbers}")
    print("\nCompare these numbers with each provider's dashboard for", day, "before committing the files.")
    return 0


def _cmd_prices(args: argparse.Namespace) -> int:
    from .app import build_pipeline
    from .upkeep import LAST_REPRICE_KEY, apply_pricing_changes, check_price_freshness

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    changed = apply_pricing_changes(pipeline, settings)
    since = (datetime.now(tz=UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    projects = [args.project] if args.project else pipeline.store.projects_with_events(since)
    for project in projects:
        print(f"project {project}")
        for r in check_price_freshness(pipeline.store, pipeline.pricer, project):
            verified = f"{r['last_verified']} ({r['age_days']}d)" if r["last_verified"] else "-"
            print(
                f"  {r['provider']:<12} {r['model']:<28} {r['status']:<15} {verified:<18} "
                f"list ${r['list_spend_usd']:.4f}  rate card ${r['rate_card_spend_usd']:.4f}"
            )
    last = changed or pipeline.store.get_state(LAST_REPRICE_KEY)
    if last:
        print("last automatic reprice:", json.dumps(last))
    return 0


def _cmd_import_vapi(args: argparse.Namespace) -> int:
    from .app import build_pipeline
    from .vapi_import import import_vapi_calls

    settings = Settings.from_env()
    pipeline = build_pipeline(settings)
    events = import_vapi_calls(args.path, project=args.project)
    result = pipeline.ingest(events, args.project)
    print(json.dumps({"events": len(events), **result}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voicetoll-collector")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the collector HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4319)
    serve.set_defaults(func=_cmd_serve)

    price = sub.add_parser("price", help="price a set of units, e.g. --unit characters=188")
    price.add_argument("--provider", required=True)
    price.add_argument("--model", required=True)
    price.add_argument("--unit", action="append", default=[], help="name=value, repeatable")
    price.add_argument("--rate-cards", default=None)
    price.set_defaults(func=_cmd_price)

    replay = sub.add_parser("replay-spool", help="load spooled batches into the database")
    replay.set_defaults(func=_cmd_replay)

    init_db = sub.add_parser("init-db", help="create tables if missing")
    init_db.set_defaults(func=_cmd_init_db)

    reprice_p = sub.add_parser("reprice", help="regenerate cost lines for a project")
    reprice_p.add_argument("--project", required=True)
    reprice_p.add_argument("--provider", default=None)
    reprice_p.add_argument("--model", default=None)
    reprice_p.add_argument("--since", default=None, help="YYYY-MM-DD")
    reprice_p.set_defaults(func=_cmd_reprice)

    rebuild = sub.add_parser(
        "rebuild-rollups", help="rebuild the daily cost rollup (cost_day) from cost lines"
    )
    rebuild.add_argument("--project", default=None)
    rebuild.add_argument("--since", default=None, help="YYYY-MM-DD")
    rebuild.set_defaults(func=_cmd_rebuild_rollups)

    recon = sub.add_parser("reconcile", help="compare estimates to provider usage APIs")
    recon.add_argument("--project", default=None, help="only accounts mapped to this project (default: all)")
    recon.add_argument("--day", default=None, help="YYYY-MM-DD (default: yesterday)")
    recon.set_defaults(func=_cmd_reconcile)

    capture = sub.add_parser(
        "recon-capture", help="save real provider usage responses (ids redacted) as contract-test fixtures"
    )
    capture.add_argument("--day", default=None, help="YYYY-MM-DD (default: yesterday)")
    capture.add_argument("--out", default="tests/fixtures/provider_usage/live")
    capture.set_defaults(func=_cmd_recon_capture)

    audit = sub.add_parser(
        "audit-call", help="compare one call with each provider's itemised usage in the call's time window"
    )
    audit.add_argument("session", help="call id (session id), as shown in the report")
    audit.add_argument("--project", default=None, help="default: the project the call was stored under")
    audit.add_argument("--pad-before", type=float, default=120.0, help="seconds before the first event")
    audit.add_argument("--pad-after", type=float, default=60.0, help="seconds after the last event")
    audit.add_argument("--json", action="store_true", help="print JSON instead of a table")
    audit.set_defaults(func=_cmd_audit_call)

    doctor = sub.add_parser("doctor", help="check the setup and provider keys, and say exactly what to fix")
    doctor.add_argument("--project", default=None, help="default: the only project with events")
    doctor.add_argument(
        "--write", action="store_true", help="save working provider settings to the recon config"
    )
    doctor.add_argument("--offline", action="store_true", help="skip calls to provider APIs")
    doctor.set_defaults(func=_cmd_doctor)

    prices_p = sub.add_parser("prices", help="check price freshness now and apply rate-card or price changes")
    prices_p.add_argument("--project", default=None)
    prices_p.set_defaults(func=_cmd_prices)

    vapi = sub.add_parser("import-vapi", help="import a Vapi call JSON dump (BYOK)")
    vapi.add_argument("path", help="path to call JSON")
    vapi.add_argument("--project", required=True)
    vapi.set_defaults(func=_cmd_import_vapi)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
