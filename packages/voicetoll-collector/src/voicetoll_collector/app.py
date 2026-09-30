"""HTTP API (Starlette).

POST /v1/events                      ingest a batch (gzip or plain JSON; header X-Voicetoll-Key)
POST /v1/otlp/v1/traces              OTLP/HTTP traces (JSON or protobuf) mapped to capture events
GET  /v1/calls?day=YYYY-MM-DD         calls on a day with minutes, turns, cost (newest first)
GET  /v1/sessions/{session_id}       cost and latency summary for one call
GET  /v1/tenants/{tenant}/daily      per-day calls, minutes, cost for a tenant (?days=7)
GET  /v1/tenants?day=YYYY-MM-DD      tenants ranked by cost for a day
GET  /v1/breakdown/{dimension}       cost by feature, component, provider, model, region... for a day
GET  /v1/highlights                  ranked findings for a project (?days=7)
GET  /v1/coverage                    drops, unpriced share, spool state for a day
GET  /v1/recon?days=14               reconciliation runs (estimate vs provider bill) per provider and day
GET  /v1/prices                      price freshness per provider/model, price versions, last automatic reprice
GET  /admin                          admin UI: prices, reports, cost, collector health (admin key)
GET  /v1/admin/{projects,prices,days,cost,health}   read-only admin views across projects (admin key)
GET  /report                         built-in HTML report over the endpoints above (no data in the page itself)
GET  /healthz                        liveness plus database and spool state
GET  /metrics                        Prometheus counters
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hmac
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .admin import HealthMonitor, cost_view, days_view, health_view, prices_view, recent_warnings
from .otlp import otlp_json_to_events, parse_otlp_body
from .pipeline import Pipeline
from .pricing import Pricer, RateCards
from .schema import ClientStats
from .settings import Settings
from .spool import Spool, SpoolFull
from .store import Store

log = logging.getLogger("voicetoll.collector")


def build_pipeline(settings: Settings) -> Pipeline:
    store = Store(settings.db_url)
    pricer = Pricer(RateCards.load(settings.rate_cards_path))
    spool = Spool(settings.spool_dir, settings.spool_max_bytes)
    return Pipeline(store, pricer, spool)


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    pipeline = pipeline or build_pipeline(settings)
    monitor = HealthMonitor()
    recent_warnings()  # start keeping collector warnings for the Health view

    def admin_key_ok(request: Request) -> bool:
        """True when the request carries the configured admin key."""
        given = request.headers.get("x-voicetoll-admin-key", "")
        return bool(settings.admin_key and given) and hmac.compare_digest(given, settings.admin_key)

    def admin_allowed(request: Request) -> bool:
        """Admin views: the admin key when one is set; open (dev) mode, with no ingest keys, needs none."""
        if settings.admin_key:
            return admin_key_ok(request)
        return not settings.ingest_keys

    def project_for(request: Request) -> str | None:
        """Return the project for the request's key; None means auth failed. Open mode when no keys are set.

        An admin key reads any project, named by ?project= (so /report works for admins without ingest keys).
        """
        if admin_key_ok(request):
            return request.query_params.get("project") or "default"
        key = request.headers.get("x-voicetoll-key")
        if not key:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                key = auth[7:].strip()
        if not settings.ingest_keys:
            return request.query_params.get("project") or "default"
        return settings.ingest_keys.get(key or "")

    def unauthorized() -> JSONResponse:
        return JSONResponse({"error": "invalid or missing ingest key"}, status_code=401)

    def save_client_stats(raw: dict[str, Any], project: str, events: list[Any], open_mode: bool) -> None:
        """Store the sending client's counters (drops, errors, buffer depth). Best-effort: never fails ingest."""
        try:
            stats = ClientStats.model_validate(raw)
        except ValidationError:
            return
        if open_mode:  # no key decides the project: use the batch's own
            first = next((e for e in events if isinstance(e, dict)), {})
            project = str(first.get("project") or project)[:64]
        sources = {str(e.get("source"))[:32] for e in events if isinstance(e, dict) and e.get("source")}
        try:
            pipeline.store.save_client_stats(
                project,
                {**stats.model_dump(), "source": ",".join(sorted(sources)) or None},
                time.time(),
                batch_events=len(events),
            )
        except Exception as exc:
            log.debug("client stats not stored: %s", exc)

    async def ingest(request: Request) -> JSONResponse:
        started = time.perf_counter()
        try:
            return await _ingest(request)
        finally:
            monitor.observe_request((time.perf_counter() - started) * 1000.0)
            monitor.sample(dict(pipeline.counters))

    async def _ingest(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            if request.headers.get("content-encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
                if len(body) > settings.max_body_bytes * 10:
                    return JSONResponse({"error": "body too large"}, status_code=413)
            payload = json.loads(body)
        except (OSError, ValueError):
            return JSONResponse({"error": "body must be JSON (optionally gzip)"}, status_code=400)
        events = payload.get("events") if isinstance(payload, dict) else payload
        if not isinstance(events, list):
            return JSONResponse({"error": 'expected {"events": [...]}'}, status_code=400)
        open_mode = not settings.ingest_keys
        client_stats = payload.get("client") if isinstance(payload, dict) else None
        try:
            result = await asyncio.to_thread(pipeline.ingest, events, None if open_mode else project)
        except SpoolFull:
            return JSONResponse({"error": "storage unavailable and spool full; retry later"}, status_code=503)
        if isinstance(client_stats, dict):  # after ingest: only batches the collector kept count as received
            await asyncio.to_thread(save_client_stats, client_stats, project, events, open_mode)
        return JSONResponse(result, status_code=202)

    async def otlp_traces(request: Request) -> Response:
        project = project_for(request)
        if project is None:
            return unauthorized()
        body = await request.body()
        if len(body) > settings.max_body_bytes:
            return JSONResponse({"error": "body too large"}, status_code=413)
        try:
            if request.headers.get("content-encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
            payload = parse_otlp_body(body, request.headers.get("content-type"))
            events = otlp_json_to_events(payload, project=project)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception:
            return JSONResponse({"error": "invalid OTLP payload"}, status_code=400)
        open_mode = not settings.ingest_keys
        try:
            result = await asyncio.to_thread(pipeline.ingest, events, None if open_mode else project)
        except SpoolFull:
            return JSONResponse({"error": "storage unavailable and spool full; retry later"}, status_code=503)
        accept = request.headers.get("accept", "")
        if "application/json" in accept:
            return JSONResponse({"partialSuccess": {}, "voicetoll": result}, status_code=200)
        return Response(status_code=200)

    async def list_calls(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        day = request.query_params.get("day") or datetime.now(tz=UTC).strftime("%Y-%m-%d")
        try:
            limit = min(max(int(request.query_params.get("limit", "500")), 1), 5000)
        except ValueError:
            return JSONResponse({"error": "limit must be an integer"}, status_code=400)
        calls = await asyncio.to_thread(pipeline.store.list_calls, project, day, limit)
        return JSONResponse({"project": project, "day": day, "calls": calls})

    async def recon_runs(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        try:
            days = min(max(int(request.query_params.get("days", "14")), 1), 366)
        except ValueError:
            return JSONResponse({"error": "days must be an integer"}, status_code=400)
        since = (datetime.now(tz=UTC) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = await asyncio.to_thread(pipeline.store.recon_runs_since, project, since)
        runs = [
            {
                "day": r["day"],
                "provider": r["provider"],
                "status": r["status"],
                "estimated_usd": r.get("estimated_usd"),
                "reported_usd": r.get("reported_usd"),
                "drift_pct": r.get("drift_pct"),
                "detail": r.get("detail") or {},
            }
            for r in rows
        ]
        return JSONResponse({"project": project, "days": days, "runs": runs})

    async def prices(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        from .upkeep import LAST_REPRICE_KEY, RATE_CARD_ERROR_KEY, VERSIONS_KEY

        def load() -> dict[str, Any]:
            store = pipeline.store
            last = store.get_state(LAST_REPRICE_KEY) or None
            if last:
                last = {**last, "projects": {project: (last.get("projects") or {}).get(project)}}
            return {
                "project": project,
                "rows": store.price_freshness_rows(project),
                "versions": store.get_state(VERSIONS_KEY),
                "last_auto_reprice": last,
                "rate_card_error": store.get_state(RATE_CARD_ERROR_KEY) or None,
                "auto_reprice_days": settings.auto_reprice_days,
            }

        return JSONResponse(await asyncio.to_thread(load))

    # ---- admin UI ------------------------------------------------------------------------------
    admin_html = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")

    def admin_denied(request: Request) -> JSONResponse | None:
        if admin_allowed(request):
            return None
        if not settings.admin_key:  # admin UI is off in a shared deploy until a key is configured
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"error": "invalid or missing admin key"}, status_code=401)

    def int_param(request: Request, name: str, default: int, low: int, high: int) -> int:
        try:
            return min(max(int(request.query_params.get(name, str(default))), low), high)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    def admin_project(request: Request) -> str | None:
        value = (request.query_params.get("project") or "").strip()
        return None if value in ("", "*", "all") else value

    async def admin_page(request: Request) -> Response:
        if not settings.admin_key and settings.ingest_keys:
            return PlainTextResponse("Not found", status_code=404)
        return HTMLResponse(admin_html, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    async def admin_projects(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        since = (datetime.now(tz=UTC) - timedelta(days=90)).strftime("%Y-%m-%d")
        seen = await asyncio.to_thread(pipeline.store.admin_projects, since)
        names = {p["project"] for p in seen}
        for project in sorted(set(settings.ingest_keys.values()) - names):
            seen.append({"project": project, "events": 0, "last_event_epoch": None})
        return JSONResponse(
            {"projects": sorted(seen, key=lambda p: p["project"]), "open_mode": not settings.ingest_keys}
        )

    async def admin_prices(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        try:
            days = int_param(request, "days", 30, 1, 366)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        body = await asyncio.to_thread(prices_view, pipeline, settings, admin_project(request), days)
        return JSONResponse(body)

    async def admin_catalog(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        try:
            days = int_param(request, "days", 30, 1, 366)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        from .catalog import catalog_view

        try:
            limit = int_param(request, "limit", 100, 1, 500)
            offset = int_param(request, "offset", 0, 0, 100_000)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        keys = ("q", "provider", "kind", "meter", "status", "in_use", "rate_card", "deprecated", "free")
        filters = {k: request.query_params.get(k, "")[:100] for k in keys}
        body = await asyncio.to_thread(catalog_view, pipeline, days, filters, limit, offset)
        return JSONResponse(body)

    async def admin_days(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        project = admin_project(request)
        if not project:
            return JSONResponse({"error": "project is required"}, status_code=400)
        try:
            days = int_param(request, "days", 14, 1, 90)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(await asyncio.to_thread(days_view, pipeline, project, days))

    async def admin_cost(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        filters = {
            col: request.query_params[param]
            for param, col in (
                ("provider", "provider"),
                ("component", "component"),
                ("model", "model"),
                ("tenant", "tenant_id"),
                ("feature", "feature"),
                ("agent_version", "agent_version"),
                ("region", "region"),
                ("env", "env"),
                ("source", "source"),
            )
            if request.query_params.get(param)
        }
        try:
            days = int_param(request, "days", 30, 1, 366)
            body = await asyncio.to_thread(
                cost_view,
                pipeline,
                admin_project(request),
                days,
                request.query_params.get("by", "component"),
                filters,
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(body)

    async def admin_health(request: Request) -> JSONResponse:
        if (denied := admin_denied(request)) is not None:
            return denied
        try:
            minutes = int_param(request, "minutes", 180, 5, 1440)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(await asyncio.to_thread(health_view, pipeline, settings, monitor, minutes))

    report_html = (Path(__file__).parent / "report.html").read_text(encoding="utf-8")

    async def report(request: Request) -> HTMLResponse:
        # The page holds no data; it calls the JSON endpoints with the viewer's key.
        return HTMLResponse(report_html, headers={"Cache-Control": "no-store"})

    async def session_summary(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        summary = await asyncio.to_thread(
            pipeline.store.session_summary, project, request.path_params["session_id"]
        )
        if summary is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return JSONResponse(summary)

    async def audit(request: Request) -> JSONResponse:
        """Per-call audit against the providers' own logs (makes provider API calls; not for dashboards)."""
        project = project_for(request)
        if project is None:
            return unauthorized()
        from .audit import audit_call

        def pad(name: str, default: float) -> float:
            try:
                return min(max(float(request.query_params.get(name, default)), 0.0), 3600.0)
            except ValueError:
                return default

        result = await asyncio.to_thread(
            audit_call, pipeline.store, project, request.path_params["session_id"],
            pad_before=pad("pad_before", 120.0), pad_after=pad("pad_after", 60.0),
        )
        if result is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    async def tenant_daily(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        days = int(request.query_params.get("days", "7"))
        since = (datetime.now(tz=UTC) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        rows = await asyncio.to_thread(
            pipeline.store.tenant_daily, project, request.path_params["tenant"], since
        )
        return JSONResponse({"tenant": request.path_params["tenant"], "days": rows})

    async def top_tenants(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        day = request.query_params.get("day") or datetime.now(tz=UTC).strftime("%Y-%m-%d")
        return JSONResponse(
            {"day": day, "tenants": await asyncio.to_thread(pipeline.store.top_tenants, project, day)}
        )

    async def breakdown(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        day = request.query_params.get("day") or datetime.now(tz=UTC).strftime("%Y-%m-%d")
        try:
            rows = await asyncio.to_thread(
                pipeline.store.cost_by, project, day, request.path_params["dimension"]
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"day": day, "dimension": request.path_params["dimension"], "rows": rows})

    async def highlights(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        days = int(request.query_params.get("days", "7"))
        from .highlights import HighlightRules, compute_highlights

        rules = HighlightRules.from_settings(settings)
        items = await asyncio.to_thread(compute_highlights, pipeline.store, project, days, rules)
        return JSONResponse({"project": project, "days": days, "highlights": items})

    async def coverage(request: Request) -> JSONResponse:
        project = project_for(request)
        if project is None:
            return unauthorized()
        day = request.query_params.get("day") or datetime.now(tz=UTC).strftime("%Y-%m-%d")
        report = await asyncio.to_thread(pipeline.store.coverage_report, project, day)
        report["spool_files"] = len(pipeline.spool.pending())
        report["counters"] = dict(pipeline.counters)
        return JSONResponse(report)

    async def healthz(request: Request) -> JSONResponse:
        db_ok = await asyncio.to_thread(pipeline.store.ping)
        return JSONResponse(
            {
                "ok": True,
                "db": db_ok,
                "spool_files": len(pipeline.spool.pending()),
                "price_version": pipeline.pricer.price_version,
                "rate_card_version": pipeline.pricer.rate_cards.version,
            }
        )

    async def metrics(request: Request) -> PlainTextResponse:
        lines = []
        for name, value in sorted(pipeline.counters.items()):
            lines.append(f"# TYPE voicetoll_{name}_total counter")
            lines.append(f"voicetoll_{name}_total {value}")
        lines.append("# TYPE voicetoll_spool_files gauge")
        lines.append(f"voicetoll_spool_files {len(pipeline.spool.pending())}")
        return PlainTextResponse("\n".join(lines) + "\n")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async def replay_loop() -> None:
            while True:
                await asyncio.sleep(settings.replay_interval_seconds)
                if pipeline.spool.pending():
                    try:
                        replayed = await asyncio.to_thread(pipeline.replay_spool)
                        if replayed:
                            log.info("replayed %d spooled events", replayed)
                        left = len(pipeline.spool.pending())
                        monitor.record_loop(
                            "replay", left == 0, {"replayed": replayed, "pending_files": left}
                        )
                    except Exception as exc:
                        log.warning("spool replay failed: %s", exc)
                        monitor.record_loop("replay", False, {"error": str(exc)[:200]})

        async def jobs_loop() -> None:
            from .jobs import run_scheduled_jobs

            # Shortly after start: freshness and highlights only (reconciliation waits for its hourly slot)
            await asyncio.sleep(5)
            try:
                result = await asyncio.to_thread(run_scheduled_jobs, pipeline, settings, reconcile=False)
                monitor.record_loop("jobs", True, result, time.time() + settings.jobs_interval_seconds)
            except Exception as exc:
                log.warning("startup jobs failed: %s", exc)
                monitor.record_loop("jobs", False, {"error": str(exc)[:200]})
            while True:
                await asyncio.sleep(settings.jobs_interval_seconds)
                try:
                    result = await asyncio.to_thread(run_scheduled_jobs, pipeline, settings)
                    monitor.record_loop("jobs", True, result, time.time() + settings.jobs_interval_seconds)
                except Exception as exc:
                    log.warning("scheduled jobs failed: %s", exc)
                    monitor.record_loop("jobs", False, {"error": str(exc)[:200]})

        async def pricing_loop() -> None:
            from .upkeep import apply_pricing_changes

            while True:
                try:
                    result = await asyncio.to_thread(apply_pricing_changes, pipeline, settings)
                    monitor.record_loop(
                        "pricing", True, result, time.time() + settings.pricing_check_interval_seconds
                    )
                except Exception as exc:
                    log.warning("pricing check failed: %s", exc)
                    monitor.record_loop("pricing", False, {"error": str(exc)[:200]})
                await asyncio.sleep(settings.pricing_check_interval_seconds)

        tasks = [
            asyncio.create_task(replay_loop()),
            asyncio.create_task(jobs_loop()),
            asyncio.create_task(pricing_loop()),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()

    routes = [
        Route("/v1/events", ingest, methods=["POST"]),
        Route("/v1/otlp/v1/traces", otlp_traces, methods=["POST"]),
        Route("/v1/calls", list_calls),
        Route("/v1/recon", recon_runs),
        Route("/v1/prices", prices),
        Route("/report", report),
        Route("/admin", admin_page),
        Route("/v1/admin/projects", admin_projects),
        Route("/v1/admin/prices", admin_prices),
        Route("/v1/admin/catalog", admin_catalog),
        Route("/v1/admin/days", admin_days),
        Route("/v1/admin/cost", admin_cost),
        Route("/v1/admin/health", admin_health),
        Route("/", lambda request: RedirectResponse("/report")),
        Route("/v1/audit/{session_id:path}", audit),
        Route("/v1/sessions/{session_id:path}", session_summary),
        Route("/v1/tenants/{tenant}/daily", tenant_daily),
        Route("/v1/tenants", top_tenants),
        Route("/v1/breakdown/{dimension}", breakdown),
        Route("/v1/highlights", highlights),
        Route("/v1/coverage", coverage),
        Route("/healthz", healthz),
        Route("/metrics", metrics),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.settings = settings
    app.state.monitor = monitor
    return app


def app_factory() -> Any:
    """Entry point for `uvicorn --factory voicetoll_collector.app:app_factory`."""
    return create_app()
