"""Storage: SQLite for local development and tests, Postgres for anything shared.

The schema uses only portable SQL (plain columns, no JSON operators), so the same queries run on both.
Raw units are immutable (`usage_event`); dollars live in `cost_line` rows that can be superseded when
prices change. Rollup tables are refreshed on ingest; summary APIs still work from raw rows as a fallback.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .pricing import CostLine
from .schema import CaptureEvent

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS usage_event (
        event_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        source TEXT,
        ts_epoch DOUBLE PRECISION NOT NULL,
        day TEXT NOT NULL,
        received_epoch DOUBLE PRECISION,
        tenant_id TEXT,
        user_id TEXT,
        session_id TEXT NOT NULL,
        turn INTEGER,
        component TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        voice_class TEXT,
        feature TEXT,
        agent_version TEXT,
        env TEXT,
        region TEXT,
        caller_country TEXT,
        status TEXT,
        cancelled INTEGER NOT NULL DEFAULT 0,
        request_id TEXT,
        ttfb_ms DOUBLE PRECISION,
        ttft_ms DOUBLE PRECISION,
        duration_ms DOUBLE PRECISION,
        eou_delay_ms DOUBLE PRECISION,
        transcription_delay_ms DOUBLE PRECISION,
        processing_ms DOUBLE PRECISION,
        units_json TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_event_session ON usage_event (project_id, session_id)",
    "CREATE INDEX IF NOT EXISTS ix_event_tenant_day ON usage_event (project_id, tenant_id, day)",
    "CREATE INDEX IF NOT EXISTS ix_event_day ON usage_event (project_id, day)",
    """
    CREATE TABLE IF NOT EXISTS cost_line (
        event_id TEXT NOT NULL,
        meter TEXT NOT NULL,
        quantity NUMERIC NOT NULL,
        unit_src TEXT,
        unit_how TEXT,
        amount_usd NUMERIC NOT NULL,
        price_source TEXT NOT NULL,
        price_version TEXT NOT NULL,
        rate_card_version TEXT NOT NULL DEFAULT '',
        freshness TEXT,
        unpriced_reason TEXT,
        is_current INTEGER NOT NULL DEFAULT 1,
        created_epoch DOUBLE PRECISION,
        PRIMARY KEY (event_id, meter, price_version, rate_card_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cost_event ON cost_line (event_id, is_current)",
    """
    CREATE TABLE IF NOT EXISTS call_rollup (
        project_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        tenant_id TEXT,
        day TEXT NOT NULL,
        minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
        cost_usd NUMERIC NOT NULL DEFAULT 0,
        events INTEGER NOT NULL DEFAULT 0,
        turns INTEGER NOT NULL DEFAULT 0,
        unpriced_count INTEGER NOT NULL DEFAULT 0,
        p50_ttfb_ms DOUBLE PRECISION,
        p95_ttfb_ms DOUBLE PRECISION,
        PRIMARY KEY (project_id, session_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tenant_day (
        project_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        day TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
        cost_usd NUMERIC NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, tenant_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_day (
        project_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        day TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0,
        minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
        cost_usd NUMERIC NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, tenant_id, user_id, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_day (
        project_id TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        feature TEXT NOT NULL,
        day TEXT NOT NULL,
        turns INTEGER NOT NULL DEFAULT 0,
        cost_usd NUMERIC NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, tenant_id, feature, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS highlight (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        rule_id TEXT NOT NULL,
        day TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT,
        dollars_at_stake NUMERIC NOT NULL DEFAULT 0,
        evidence_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_epoch DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_highlight_project ON highlight (project_id, day)",
    """
    CREATE TABLE IF NOT EXISTS recon_run (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        day TEXT NOT NULL,
        estimated_usd NUMERIC NOT NULL,
        reported_usd NUMERIC,
        drift_pct DOUBLE PRECISION,
        status TEXT NOT NULL,
        detail_json TEXT,
        created_epoch DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_recon_project_day ON recon_run (project_id, day, provider)",
    """
    CREATE TABLE IF NOT EXISTS price_freshness (
        project_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence TEXT,
        last_verified TEXT,
        age_days INTEGER,
        threshold_days INTEGER,
        list_spend_usd NUMERIC NOT NULL DEFAULT 0,
        rate_card_spend_usd NUMERIC NOT NULL DEFAULT 0,
        events INTEGER NOT NULL DEFAULT 0,
        checked_day TEXT NOT NULL,
        checked_epoch DOUBLE PRECISION,
        PRIMARY KEY (project_id, provider, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cost_day (
        project_id TEXT NOT NULL,
        day TEXT NOT NULL,
        component TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        feature TEXT NOT NULL DEFAULT '',
        agent_version TEXT NOT NULL DEFAULT '',
        region TEXT NOT NULL DEFAULT '',
        env TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT '',
        price_source TEXT NOT NULL,
        cost_usd NUMERIC NOT NULL DEFAULT 0,
        lines INTEGER NOT NULL DEFAULT 0,
        unpriced_lines INTEGER NOT NULL DEFAULT 0,
        events INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, day, component, provider, model, feature, agent_version, region, env, source,
                     price_source)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_cost_day_day ON cost_day (day, project_id)",
    """
    CREATE TABLE IF NOT EXISTS client_stats (
        project_id TEXT NOT NULL,
        client_id TEXT NOT NULL,
        sdk_version TEXT,
        source TEXT,
        dropped INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        sent INTEGER NOT NULL DEFAULT 0,
        buffer_len INTEGER NOT NULL DEFAULT 0,
        buffer_max INTEGER NOT NULL DEFAULT 0,
        started_epoch DOUBLE PRECISION,
        reported_epoch DOUBLE PRECISION NOT NULL,
        received INTEGER NOT NULL DEFAULT 0,
        received_before INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (project_id, client_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collector_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_epoch DOUBLE PRECISION
    )
    """,
]

EVENT_COLUMNS = (
    "event_id",
    "project_id",
    "source",
    "ts_epoch",
    "day",
    "received_epoch",
    "tenant_id",
    "user_id",
    "session_id",
    "turn",
    "component",
    "provider",
    "model",
    "voice_class",
    "feature",
    "agent_version",
    "env",
    "region",
    "caller_country",
    "status",
    "cancelled",
    "request_id",
    "ttfb_ms",
    "ttft_ms",
    "duration_ms",
    "eou_delay_ms",
    "transcription_delay_ms",
    "processing_ms",
    "units_json",
)
LINE_COLUMNS = (
    "event_id",
    "meter",
    "quantity",
    "unit_src",
    "unit_how",
    "amount_usd",
    "price_source",
    "price_version",
    "rate_card_version",
    "freshness",
    "unpriced_reason",
    "is_current",
    "created_epoch",
)


def event_row(event: CaptureEvent) -> dict[str, Any]:
    t = event.timing_ms
    return {
        "event_id": event.event_id,
        "project_id": event.project,
        "source": event.source,
        "ts_epoch": event.ts.timestamp(),
        "day": event.day,
        "received_epoch": time.time(),
        "tenant_id": event.tenant,
        "user_id": event.user,
        "session_id": event.session,
        "turn": event.turn,
        "component": event.component,
        "provider": event.provider,
        "model": event.model,
        "voice_class": event.voice_class,
        "feature": event.tags.get("feature"),
        "agent_version": event.tags.get("agent_version"),
        "env": event.tags.get("env"),
        "region": event.tags.get("region"),
        "caller_country": event.tags.get("caller_country"),
        "status": event.status,
        "cancelled": 1 if event.cancelled else 0,
        "request_id": event.request_id,
        "ttfb_ms": t.get("ttfb"),
        "ttft_ms": t.get("ttft"),
        "duration_ms": t.get("duration"),
        "eou_delay_ms": t.get("eou_delay"),
        "transcription_delay_ms": t.get("transcription_delay"),
        "processing_ms": t.get("processing"),
        "units_json": json.dumps(
            {
                k: {"v": v, "src": event.unit_source(k), "how": event.how.get(k)}
                for k, v in event.units.items()
            },
            separators=(",", ":"),
        ),
    }


def line_row(event_id: str, line: CostLine) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "meter": line.meter,
        "quantity": str(line.quantity),
        "unit_src": line.unit_src,
        "unit_how": line.unit_how,
        "amount_usd": str(line.amount_usd),
        "price_source": line.price_source,
        "price_version": line.price_version,
        "rate_card_version": line.rate_card_version or "",
        "freshness": line.freshness,
        "unpriced_reason": line.unpriced_reason,
        "is_current": 1,
        "created_epoch": time.time(),
    }


# cost_day: one row per project, day, dimension combination and price source. Empty string stands for
# "not set" so the primary key (and ON CONFLICT upserts) work the same on SQLite and Postgres.
COST_DAY_DIMS = ("component", "provider", "model", "feature", "agent_version", "region", "env", "source")
COST_DAY_KEY = ("project_id", "day", *COST_DAY_DIMS, "price_source")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return round(values[0], 1)
    cuts = statistics.quantiles(sorted(values), n=100, method="inclusive")
    return round(cuts[int(q) - 1], 1)


def _num(value: Any) -> float:
    if value is None:
        return 0.0
    return float(Decimal(str(value)))


@dataclass
class InsertResult:
    inserted: int
    duplicates: int


class Store:
    def __init__(self, url: str) -> None:
        self.url = url
        self._lock = threading.Lock()
        self._dirty_cost_days: set[tuple[str, str]] = set()  # days whose cost lines were superseded
        if url.startswith("sqlite:///"):
            self.dialect = "sqlite"
            path = url[len("sqlite:///") :] or ":memory:"
            self._conn: Any = sqlite3.connect(path, check_same_thread=False)
        elif url.startswith(("postgresql://", "postgres://")):
            self.dialect = "postgres"
            import psycopg  # optional dependency: pip install "voicetoll-collector[postgres]"

            self._conn = psycopg.connect(url)
        else:
            raise ValueError(f"unsupported VOICETOLL_DB_URL: {url!r}")
        self.init_schema()

    # ---- low level -------------------------------------------------------------------------
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()] if cols else []
            if self.dialect == "postgres":
                self._conn.commit()
            return rows

    def init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for statement in SCHEMA:
                cur.execute(statement)
            self._conn.commit()
        self._add_missing_columns()

    # Columns added after a table first shipped: (table, column, type). Fresh installs get them from SCHEMA.
    ADDED_COLUMNS = (
        ("client_stats", "received", "INTEGER NOT NULL DEFAULT 0"),
        ("client_stats", "received_before", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _add_missing_columns(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            for table, column, ddl in self.ADDED_COLUMNS:
                if self.dialect == "postgres":
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
                else:
                    cur.execute(f"PRAGMA table_info({table})")
                    if column not in {row[1] for row in cur.fetchall()}:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            self._conn.commit()

    def ping(self) -> bool:
        try:
            self._query("SELECT 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- writes ----------------------------------------------------------------------------
    def insert_batch(self, items: list[tuple[CaptureEvent, list[CostLine]]]) -> InsertResult:
        """Insert events and their cost lines in one transaction. Idempotent on event_id."""
        ev_sql = self._sql(
            f"INSERT INTO usage_event ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' * len(EVENT_COLUMNS))}) "
            "ON CONFLICT (event_id) DO NOTHING"
        )
        line_sql = self._sql(
            f"INSERT INTO cost_line ({', '.join(LINE_COLUMNS)}) VALUES ({', '.join('?' * len(LINE_COLUMNS))}) "
            "ON CONFLICT DO NOTHING"
        )
        inserted = duplicates = 0
        touched_sessions: set[tuple[str, str]] = set()
        deltas: dict[tuple, list[float]] = {}  # cost_day key -> [cost, lines, unpriced, events]
        with self._lock:
            cur = self._conn.cursor()
            try:
                for event, lines in items:
                    row = event_row(event)
                    cur.execute(ev_sql, tuple(row[c] for c in EVENT_COLUMNS))
                    if cur.rowcount == 0:
                        duplicates += 1
                        continue
                    inserted += 1
                    seen_keys: set[tuple] = set()
                    for line in lines:
                        lr = line_row(event.event_id, line)
                        cur.execute(line_sql, tuple(lr[c] for c in LINE_COLUMNS))
                        key = (row["project_id"], row["day"], *(row[d] or "" for d in COST_DAY_DIMS), line.price_source)
                        acc = deltas.setdefault(key, [0.0, 0, 0, 0])
                        acc[0] += float(line.amount_usd)
                        acc[1] += 1
                        acc[2] += 1 if line.price_source == "unpriced" else 0
                        if key not in seen_keys:
                            acc[3] += 1
                            seen_keys.add(key)
                    touched_sessions.add((event.project, event.session))
                if deltas:
                    cur.executemany(self._sql(self._COST_DAY_UPSERT), [(*k, *v) for k, v in deltas.items()])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        for project, session in touched_sessions:
            try:
                self.refresh_call_rollups(project, session)
            except Exception:
                pass  # rollup refresh is best-effort; raw rows remain authoritative
        return InsertResult(inserted, duplicates)

    # ---- reads -----------------------------------------------------------------------------
    def session_summary(self, project: str, session_id: str) -> dict[str, Any] | None:
        events = self._query(
            "SELECT * FROM usage_event WHERE project_id = ? AND session_id = ? ORDER BY ts_epoch",
            (project, session_id),
        )
        if not events:
            return None
        lines = self._query(
            "SELECT c.* FROM cost_line c JOIN usage_event e ON e.event_id = c.event_id "
            "WHERE e.project_id = ? AND e.session_id = ? AND c.is_current = 1",
            (project, session_id),
        )
        by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for line in lines:
            by_event[line["event_id"]].append(line)

        components: dict[str, dict[str, Any]] = {}
        latency: dict[str, list[float]] = defaultdict(list)
        turns: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        total = 0.0
        unpriced = 0
        for ev in events:
            comp = components.setdefault(
                ev["component"],
                {
                    "provider": ev["provider"],
                    "model": ev["model"],
                    "cost_usd": 0.0,
                    "units": defaultdict(float),
                },
            )
            for unit, info in json.loads(ev["units_json"]).items():
                comp["units"][unit] += float(info["v"])
            for line in by_event.get(ev["event_id"], []):
                amount = _num(line["amount_usd"])
                comp["cost_usd"] += amount
                total += amount
                if ev["turn"] is not None:
                    turns[ev["turn"]][ev["component"]] += amount
                if line["price_source"] == "unpriced":
                    unpriced += 1
            for metric in ("ttfb_ms", "ttft_ms", "eou_delay_ms", "transcription_delay_ms", "duration_ms"):
                if ev[metric] is not None:
                    latency[f"{ev['component']}.{metric[:-3]}"].append(float(ev[metric]))
        started, ended = events[0]["ts_epoch"], events[-1]["ts_epoch"]
        minutes = max((ended - started) / 60.0, 0.0)
        for comp in components.values():
            comp["cost_usd"] = round(comp["cost_usd"], 8)
            comp["units"] = {k: round(v, 3) for k, v in comp["units"].items()}
        return {
            "session_id": session_id,
            "tenant_id": events[0]["tenant_id"],
            "events": len(events),
            "turns": len({e["turn"] for e in events if e["turn"] is not None}),
            "started_epoch": started,
            "minutes": round(minutes, 3),
            "cost_usd": round(total, 8),
            "cost_per_minute": round(total / minutes, 8) if minutes > 0 else None,
            "unpriced_lines": unpriced,
            "components": components,
            "cost_by_turn": {t: {k: round(v, 8) for k, v in c.items()} for t, c in sorted(turns.items())},
            "latency_ms": {
                k: {"p50": _pct(v, 50), "p95": _pct(v, 95), "n": len(v)} for k, v in sorted(latency.items())
            },
        }

    def list_calls(self, project: str, day: str, limit: int = 500) -> list[dict[str, Any]]:
        """Calls with at least one event on `day`, newest first: timing, turns, cost and unpriced lines."""
        rows = self._query(
            "SELECT session_id, MIN(tenant_id) AS tenant_id, MIN(feature) AS feature, "
            "MIN(agent_version) AS agent_version, MIN(ts_epoch) AS started, MAX(ts_epoch) AS ended, "
            "COUNT(*) AS events, COUNT(DISTINCT turn) AS turns "
            "FROM usage_event WHERE project_id = ? AND day = ? GROUP BY session_id "
            "ORDER BY MIN(ts_epoch) DESC LIMIT ?",
            (project, day, int(limit)),
        )
        if not rows:
            return []
        costs = self._query(
            "SELECT e.session_id AS session_id, COALESCE(SUM(c.amount_usd), 0) AS cost_usd, "
            "SUM(CASE WHEN c.price_source = 'unpriced' THEN 1 ELSE 0 END) AS unpriced "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            "WHERE e.project_id = ? AND e.day = ? GROUP BY e.session_id",
            (project, day),
        )
        by_session = {r["session_id"]: r for r in costs}
        out = []
        for row in rows:
            cost = _num((by_session.get(row["session_id"]) or {}).get("cost_usd"))
            minutes = max((float(row["ended"]) - float(row["started"])) / 60.0, 0.0)
            out.append(
                {
                    "session_id": row["session_id"],
                    "tenant_id": row["tenant_id"],
                    "feature": row["feature"],
                    "agent_version": row["agent_version"],
                    "started_epoch": float(row["started"]),
                    "minutes": round(minutes, 3),
                    "events": int(row["events"]),
                    "turns": int(row["turns"] or 0),
                    "cost_usd": round(cost, 8),
                    "cost_per_minute": round(cost / minutes, 8) if minutes > 0 else None,
                    "unpriced_lines": int(_num((by_session.get(row["session_id"]) or {}).get("unpriced"))),
                }
            )
        return out

    def tenant_daily(self, project: str, tenant: str, since_day: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT e.day AS day, COUNT(DISTINCT e.session_id) AS calls, "
            "COALESCE(SUM(c.amount_usd), 0) AS cost_usd "
            "FROM usage_event e LEFT JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            "WHERE e.project_id = ? AND e.tenant_id = ? AND e.day >= ? GROUP BY e.day ORDER BY e.day",
            (project, tenant, since_day),
        )
        spans = self._query(
            "SELECT day, session_id, MIN(ts_epoch) AS s, MAX(ts_epoch) AS e FROM usage_event "
            "WHERE project_id = ? AND tenant_id = ? AND day >= ? GROUP BY day, session_id",
            (project, tenant, since_day),
        )
        minutes: dict[str, float] = defaultdict(float)
        for span in spans:
            minutes[span["day"]] += max((span["e"] - span["s"]) / 60.0, 0.0)
        out = []
        for row in rows:
            cost = _num(row["cost_usd"])
            mins = minutes.get(row["day"], 0.0)
            out.append(
                {
                    "day": row["day"],
                    "calls": row["calls"],
                    "cost_usd": round(cost, 8),
                    "minutes": round(mins, 3),
                    "cost_per_minute": round(cost / mins, 8) if mins > 0 else None,
                }
            )
        return out

    def top_tenants(self, project: str, day: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT e.tenant_id AS tenant_id, COUNT(DISTINCT e.session_id) AS calls, "
            "COALESCE(SUM(c.amount_usd), 0) AS cost_usd "
            "FROM usage_event e LEFT JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            "WHERE e.project_id = ? AND e.day = ? GROUP BY e.tenant_id ORDER BY cost_usd DESC",
            (project, day),
        )
        return [
            {"tenant_id": r["tenant_id"], "calls": r["calls"], "cost_usd": round(_num(r["cost_usd"]), 8)}
            for r in rows[:limit]
        ]

    def cost_by(self, project: str, day: str, dimension: str) -> list[dict[str, Any]]:
        if dimension not in {
            "feature",
            "component",
            "provider",
            "model",
            "region",
            "agent_version",
            "user_id",
        }:
            raise ValueError(f"unsupported dimension: {dimension}")
        rows = self._query(
            f"SELECT e.{dimension} AS key, COALESCE(SUM(c.amount_usd), 0) AS cost_usd, COUNT(DISTINCT e.session_id) AS calls "
            "FROM usage_event e LEFT JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            f"WHERE e.project_id = ? AND e.day = ? GROUP BY e.{dimension} ORDER BY cost_usd DESC",
            (project, day),
        )
        return [
            {"key": r["key"], "calls": r["calls"], "cost_usd": round(_num(r["cost_usd"]), 8)} for r in rows
        ]

    # ---- rollups / coverage / recon / highlights -------------------------------------------
    def refresh_call_rollups(self, project: str, session_id: str) -> None:
        summary = self.session_summary(project, session_id)
        if summary is None:
            return
        day_rows = self._query(
            "SELECT day FROM usage_event WHERE project_id = ? AND session_id = ? ORDER BY ts_epoch LIMIT 1",
            (project, session_id),
        )
        day = day_rows[0]["day"] if day_rows else ""
        tenant = summary.get("tenant_id")
        ttfb = summary.get("latency_ms", {}).get("tts.ttfb") or {}
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                self._sql(
                    "INSERT INTO call_rollup (project_id, session_id, tenant_id, day, minutes, cost_usd, events, turns, "
                    "unpriced_count, p50_ttfb_ms, p95_ttfb_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (project_id, session_id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id, day=EXCLUDED.day, "
                    "minutes=EXCLUDED.minutes, cost_usd=EXCLUDED.cost_usd, events=EXCLUDED.events, turns=EXCLUDED.turns, "
                    "unpriced_count=EXCLUDED.unpriced_count, p50_ttfb_ms=EXCLUDED.p50_ttfb_ms, p95_ttfb_ms=EXCLUDED.p95_ttfb_ms"
                    if self.dialect == "postgres"
                    else "INSERT OR REPLACE INTO call_rollup (project_id, session_id, tenant_id, day, minutes, cost_usd, "
                    "events, turns, unpriced_count, p50_ttfb_ms, p95_ttfb_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                ),
                (
                    project,
                    session_id,
                    tenant,
                    day,
                    summary["minutes"],
                    summary["cost_usd"],
                    summary["events"],
                    summary["turns"],
                    summary["unpriced_lines"],
                    ttfb.get("p50"),
                    ttfb.get("p95"),
                ),
            )
            self._conn.commit()
        if tenant and day:
            self._refresh_tenant_day(project, tenant, day)
            self._refresh_user_feature_day(project, tenant, day, session_id)

    def _upsert_day(self, table: str, keys: list[str], values: dict[str, Any]) -> None:
        cols = keys + [k for k in values if k not in keys]
        placeholders = ", ".join("?" * len(cols))
        col_list = ", ".join(cols)
        if self.dialect == "postgres":
            updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in values if c not in keys)
            sql = (
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {updates}"
            )
        else:
            sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"
        params = tuple(values[c] if c in values else values.get(c) for c in cols)
        # keys come from values too
        params = tuple(values[c] for c in cols)
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), params)
            self._conn.commit()

    def _refresh_tenant_day(self, project: str, tenant: str, day: str) -> None:
        rows = self.tenant_daily(project, tenant, day)
        match = next((r for r in rows if r["day"] == day), None)
        if not match:
            return
        self._upsert_day(
            "tenant_day",
            ["project_id", "tenant_id", "day"],
            {
                "project_id": project,
                "tenant_id": tenant,
                "day": day,
                "calls": match["calls"],
                "minutes": match["minutes"],
                "cost_usd": match["cost_usd"],
            },
        )

    def _refresh_user_feature_day(self, project: str, tenant: str, day: str, session_id: str) -> None:
        events = self._query(
            "SELECT user_id, feature, turn FROM usage_event WHERE project_id = ? AND session_id = ?",
            (project, session_id),
        )
        users = {e["user_id"] for e in events if e["user_id"]}
        features = {e["feature"] for e in events if e["feature"]}
        for user in users:
            cost_rows = self._query(
                "SELECT COALESCE(SUM(c.amount_usd), 0) AS cost_usd, COUNT(DISTINCT e.session_id) AS calls "
                "FROM usage_event e LEFT JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
                "WHERE e.project_id = ? AND e.tenant_id = ? AND e.user_id = ? AND e.day = ?",
                (project, tenant, user, day),
            )
            cost = _num(cost_rows[0]["cost_usd"]) if cost_rows else 0.0
            calls = cost_rows[0]["calls"] if cost_rows else 0
            self._upsert_day(
                "user_day",
                ["project_id", "tenant_id", "user_id", "day"],
                {
                    "project_id": project,
                    "tenant_id": tenant,
                    "user_id": user,
                    "day": day,
                    "calls": calls,
                    "minutes": 0.0,
                    "cost_usd": cost,
                },
            )
        for feature in features:
            cost_rows = self._query(
                "SELECT COALESCE(SUM(c.amount_usd), 0) AS cost_usd, COUNT(DISTINCT e.turn) AS turns "
                "FROM usage_event e LEFT JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
                "WHERE e.project_id = ? AND e.tenant_id = ? AND e.feature = ? AND e.day = ?",
                (project, tenant, feature, day),
            )
            cost = _num(cost_rows[0]["cost_usd"]) if cost_rows else 0.0
            turns = cost_rows[0]["turns"] if cost_rows else 0
            self._upsert_day(
                "feature_day",
                ["project_id", "tenant_id", "feature", "day"],
                {
                    "project_id": project,
                    "tenant_id": tenant,
                    "feature": feature,
                    "day": day,
                    "turns": turns,
                    "cost_usd": cost,
                },
            )

    def coverage_report(self, project: str, day: str) -> dict[str, Any]:
        events = self._query(
            "SELECT COUNT(*) AS n FROM usage_event WHERE project_id = ? AND day = ?", (project, day)
        )
        lines = self._query(
            "SELECT c.price_source AS price_source, COUNT(*) AS n FROM cost_line c "
            "JOIN usage_event e ON e.event_id = c.event_id "
            "WHERE e.project_id = ? AND e.day = ? AND c.is_current = 1 GROUP BY c.price_source",
            (project, day),
        )
        by_source = {r["price_source"]: r["n"] for r in lines}
        total_lines = sum(by_source.values()) or 1
        return {
            "day": day,
            "project": project,
            "events": events[0]["n"] if events else 0,
            "cost_lines": total_lines if lines else 0,
            "unpriced_share": round(by_source.get("unpriced", 0) / total_lines, 4),
            "not_billed_share": round(by_source.get("not_billed", 0) / total_lines, 4),
            "by_price_source": by_source,
        }

    def list_events_for_reprice(
        self,
        project: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        since_day: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project]
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if since_day:
            clauses.append("day >= ?")
            params.append(since_day)
        return self._query(
            f"SELECT * FROM usage_event WHERE {' AND '.join(clauses)} ORDER BY ts_epoch", tuple(params)
        )

    def supersede_cost_lines(self, event_id: str, new_lines: list[CostLine]) -> None:
        """Mark current lines stale and insert replacements under a distinct version key."""
        bump = f"reprice-{int(time.time() * 1000)}"
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                self._sql("UPDATE cost_line SET is_current = 0 WHERE event_id = ? AND is_current = 1"),
                (event_id,),
            )
            line_sql = self._sql(
                f"INSERT INTO cost_line ({', '.join(LINE_COLUMNS)}) VALUES ({', '.join('?' * len(LINE_COLUMNS))}) "
                "ON CONFLICT DO NOTHING"
            )
            for line in new_lines:
                lr = line_row(event_id, line)
                base = lr["rate_card_version"] or "default"
                lr["rate_card_version"] = f"{base}+{bump}"
                cur.execute(line_sql, tuple(lr[c] for c in LINE_COLUMNS))
            cur.execute(self._sql("SELECT project_id, day FROM usage_event WHERE event_id = ?"), (event_id,))
            found = cur.fetchone()
            self._conn.commit()
        if found:
            self._dirty_cost_days.add((found[0], found[1]))

    # ---- price freshness and collector state ------------------------------------------------
    def spend_by_model(self, project: str, since_day: str) -> list[dict[str, Any]]:
        """Current cost per provider/model/price source since a day (drives the freshness check)."""
        rows = self._query(
            "SELECT e.provider AS provider, e.model AS model, c.price_source AS price_source, "
            "COALESCE(SUM(c.amount_usd), 0) AS cost_usd, COUNT(DISTINCT e.event_id) AS events "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            "WHERE e.project_id = ? AND e.day >= ? AND e.provider IS NOT NULL AND e.model IS NOT NULL "
            "GROUP BY e.provider, e.model, c.price_source",
            (project, since_day),
        )
        for row in rows:
            row["cost_usd"] = _num(row["cost_usd"])
        return rows

    def save_price_freshness(self, project: str, rows: list[dict[str, Any]]) -> None:
        cols = (
            "project_id", "provider", "model", "status", "confidence", "last_verified", "age_days",
            "threshold_days", "list_spend_usd", "rate_card_spend_usd", "events", "checked_day", "checked_epoch",
        )
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql("DELETE FROM price_freshness WHERE project_id = ?"), (project,))
            sql = self._sql(f"INSERT INTO price_freshness ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})")
            for row in rows:
                cur.execute(sql, tuple({**row, "project_id": project}.get(c) for c in cols))
            self._conn.commit()

    def price_freshness_rows(self, project: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM price_freshness WHERE project_id = ? ORDER BY list_spend_usd DESC, provider, model",
            (project,),
        )
        for row in rows:
            row["list_spend_usd"] = _num(row["list_spend_usd"])
            row["rate_card_spend_usd"] = _num(row["rate_card_spend_usd"])
        return rows

    def get_state(self, key: str) -> Any:
        rows = self._query("SELECT value FROM collector_state WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else None

    def set_state(self, key: str, value: Any) -> None:
        sql = (
            "INSERT INTO collector_state (key, value, updated_epoch) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_epoch = EXCLUDED.updated_epoch"
        )
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql(sql), (key, json.dumps(value), time.time()))
            self._conn.commit()

    def current_lines_by_event(self, event_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(event_ids), 500):
            chunk = event_ids[i : i + 500]
            rows = self._query(
                f"SELECT event_id, meter, amount_usd, price_source FROM cost_line "
                f"WHERE is_current = 1 AND event_id IN ({', '.join('?' * len(chunk))})",
                tuple(chunk),
            )
            for row in rows:
                out.setdefault(row["event_id"], []).append(row)
        return out

    def projects_with_events(self, since_day: str) -> list[str]:
        rows = self._query("SELECT DISTINCT project_id AS project_id FROM usage_event WHERE day >= ?", (since_day,))
        return [r["project_id"] for r in rows]

    def save_highlights(self, project: str, day: str, items: list[dict[str, Any]]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(self._sql("DELETE FROM highlight WHERE project_id = ? AND day = ?"), (project, day))
            for item in items:
                # A finding keeps its id across days (same rule, same subject): move it to today instead of
                # colliding with yesterday's row, which made /v1/highlights fail after midnight UTC.
                cur.execute(self._sql("DELETE FROM highlight WHERE id = ?"), (item["id"],))
                cur.execute(
                    self._sql(
                        "INSERT INTO highlight (id, project_id, rule_id, day, title, detail, dollars_at_stake, "
                        "evidence_json, status, created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?)"
                    ),
                    (
                        item["id"],
                        project,
                        item["rule_id"],
                        day,
                        item["title"],
                        item.get("detail"),
                        item.get("dollars_at_stake", 0),
                        json.dumps(item.get("evidence") or {}),
                        item.get("status", "open"),
                        time.time(),
                    ),
                )
            self._conn.commit()

    def list_highlights(self, project: str, since_day: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM highlight WHERE project_id = ? AND day >= ? ORDER BY dollars_at_stake DESC",
            (project, since_day),
        )
        out = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "rule_id": row["rule_id"],
                    "day": row["day"],
                    "title": row["title"],
                    "detail": row["detail"],
                    "dollars_at_stake": round(_num(row["dollars_at_stake"]), 4),
                    "evidence": json.loads(row["evidence_json"] or "{}"),
                    "status": row["status"],
                }
            )
        return out

    def save_recon_run(self, row: dict[str, Any]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                self._sql(
                    "INSERT INTO recon_run (id, project_id, provider, day, estimated_usd, reported_usd, drift_pct, "
                    "status, detail_json, created_epoch) VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT (id) DO UPDATE SET estimated_usd = excluded.estimated_usd, "
                    "reported_usd = excluded.reported_usd, drift_pct = excluded.drift_pct, "
                    "status = excluded.status, detail_json = excluded.detail_json, "
                    "created_epoch = excluded.created_epoch"
                ),
                (
                    row["id"],
                    row["project_id"],
                    row["provider"],
                    row["day"],
                    row["estimated_usd"],
                    row.get("reported_usd"),
                    row.get("drift_pct"),
                    row["status"],
                    json.dumps(row.get("detail") or {}),
                    time.time(),
                ),
            )
            self._conn.commit()

    def estimated_provider_units(self, project: str, provider: str, day: str) -> dict[str, float]:
        """Sum of measured quantities per meter (all price sources, including not_billed/unpriced)."""
        rows = self._query(
            "SELECT c.meter AS meter, COALESCE(SUM(c.quantity), 0) AS quantity FROM cost_line c "
            "JOIN usage_event e ON e.event_id = c.event_id "
            "WHERE e.project_id = ? AND e.provider = ? AND e.day = ? AND c.is_current = 1 GROUP BY c.meter",
            (project, provider, day),
        )
        return {r["meter"]: _num(r["quantity"]) for r in rows}

    def recon_run_for(self, project: str, provider: str, day: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM recon_run WHERE project_id = ? AND provider = ? AND day = ?", (project, provider, day)
        )
        if not rows:
            return None
        row = dict(rows[0])
        row["detail"] = json.loads(row.get("detail_json") or "{}")
        return row

    def recon_runs_since(self, project: str, since_day: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM recon_run WHERE project_id = ? AND day >= ? ORDER BY day DESC, provider",
            (project, since_day),
        )
        for row in rows:
            row["detail"] = json.loads(row.get("detail_json") or "{}")
        return rows

    def estimated_provider_day(self, project: str, provider: str, day: str) -> float:
        rows = self._query(
            "SELECT COALESCE(SUM(c.amount_usd), 0) AS cost_usd FROM cost_line c "
            "JOIN usage_event e ON e.event_id = c.event_id "
            "WHERE e.project_id = ? AND e.provider = ? AND e.day = ? AND c.is_current = 1",
            (project, provider, day),
        )
        return _num(rows[0]["cost_usd"]) if rows else 0.0

    def tenant_day_rows(self, project: str, since_day: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM tenant_day WHERE project_id = ? AND day >= ? ORDER BY cost_usd DESC",
            (project, since_day),
        )

    def call_rollup_rows(self, project: str, since_day: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM call_rollup WHERE project_id = ? AND day >= ? ORDER BY cost_usd DESC",
            (project, since_day),
        )

    # ---- admin UI (read-only, all projects) -------------------------------------------------
    ADMIN_FILTER_COLUMNS = (
        "provider", "component", "model", "tenant_id", "feature", "agent_version", "region", "env", "source",
    )

    def _admin_where(
        self, project: str | None, since_day: str, until_day: str | None, filters: dict[str, str] | None
    ) -> tuple[str, list[Any]]:
        """WHERE clause on usage_event `e` for the admin views. Filter keys are checked against a fixed list."""
        clauses, params = ["e.day >= ?"], [since_day]
        if until_day:
            clauses.append("e.day <= ?")
            params.append(until_day)
        if project:
            clauses.append("e.project_id = ?")
            params.append(project)
        for column, value in (filters or {}).items():
            if column not in self.ADMIN_FILTER_COLUMNS:
                raise ValueError(f"unsupported filter: {column}")
            if value in (None, ""):
                continue
            clauses.append(f"e.{column} = ?")
            params.append(value)
        return " AND ".join(clauses), params

    def admin_projects(self, since_day: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT project_id, COUNT(*) AS events, MAX(received_epoch) AS last_received, MAX(ts_epoch) AS last_ts "
            "FROM usage_event WHERE day >= ? GROUP BY project_id ORDER BY project_id",
            (since_day,),
        )
        return [
            {
                "project": r["project_id"],
                "events": int(r["events"]),
                "last_event_epoch": float(r["last_received"] or r["last_ts"] or 0) or None,
            }
            for r in rows
        ]

    def admin_price_rows(self, project: str | None, since_day: str) -> list[dict[str, Any]]:
        """Current cost lines grouped by provider, model, meter and price source (what traffic actually used)."""
        where, params = self._admin_where(project, since_day, None, None)
        rows = self._query(
            "SELECT e.provider AS provider, e.model AS model, MIN(e.component) AS component, c.meter AS meter, "
            "c.price_source AS price_source, MIN(c.unpriced_reason) AS unpriced_reason, "
            "MAX(c.rate_card_version) AS rate_card_version, COALESCE(SUM(c.amount_usd), 0) AS cost_usd, "
            "COALESCE(SUM(c.quantity), 0) AS quantity, COUNT(DISTINCT e.event_id) AS events, "
            "MIN(e.ts_epoch) AS first_ts, MAX(e.ts_epoch) AS last_ts "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            f"WHERE {where} GROUP BY e.provider, e.model, c.meter, c.price_source ORDER BY cost_usd DESC",
            tuple(params),
        )
        for row in rows:
            row["cost_usd"] = _num(row["cost_usd"])
            row["quantity"] = _num(row["quantity"])
            row["events"] = int(row["events"])
        return rows

    def admin_sessions(
        self,
        project: str | None,
        since_day: str,
        until_day: str | None = None,
        filters: dict[str, str] | None = None,
        until_epoch: float | None = None,
    ) -> list[dict[str, Any]]:
        """One row per call in the range: day, tenant, span, cost and cost-line counts (filtered events only)."""
        where, params = self._admin_where(project, since_day, until_day, filters)
        if until_epoch is not None:
            where += " AND e.ts_epoch <= ?"
            params.append(until_epoch)
        spans = self._query(
            "SELECT e.project_id AS project_id, e.session_id AS session_id, MIN(e.day) AS day, "
            "MIN(e.tenant_id) AS tenant_id, MIN(e.ts_epoch) AS started, MAX(e.ts_epoch) AS ended "
            f"FROM usage_event e WHERE {where} GROUP BY e.project_id, e.session_id",
            tuple(params),
        )
        costs = self._query(
            "SELECT e.project_id AS project_id, e.session_id AS session_id, COALESCE(SUM(c.amount_usd), 0) AS cost_usd, "
            "COUNT(c.event_id) AS lines, SUM(CASE WHEN c.price_source = 'unpriced' THEN 1 ELSE 0 END) AS unpriced "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            f"WHERE {where} GROUP BY e.project_id, e.session_id",
            tuple(params),
        )
        by_key = {(r["project_id"], r["session_id"]): r for r in costs}
        out = []
        for s in spans:
            c = by_key.get((s["project_id"], s["session_id"])) or {}
            out.append(
                {
                    "project": s["project_id"],
                    "session_id": s["session_id"],
                    "day": s["day"],
                    "tenant_id": s["tenant_id"],
                    "minutes": max((float(s["ended"]) - float(s["started"])) / 60.0, 0.0),
                    "cost_usd": _num(c.get("cost_usd")),
                    "lines": int(_num(c.get("lines"))),
                    "unpriced_lines": int(_num(c.get("unpriced"))),
                }
            )
        return out

    def admin_cost_by_day(
        self,
        project: str | None,
        since_day: str,
        until_day: str | None,
        dimension: str,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if dimension not in self.ADMIN_FILTER_COLUMNS:
            raise ValueError(f"unsupported dimension: {dimension}")
        where, params = self._admin_where(project, since_day, until_day, filters)
        rows = self._query(
            f"SELECT e.day AS day, e.{dimension} AS key, COALESCE(SUM(c.amount_usd), 0) AS cost_usd "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            f"WHERE {where} GROUP BY e.day, e.{dimension} ORDER BY e.day",
            tuple(params),
        )
        return [{"day": r["day"], "key": r["key"], "cost_usd": round(_num(r["cost_usd"]), 8)} for r in rows]

    def admin_cost_by(
        self,
        project: str | None,
        since_day: str,
        until_day: str | None,
        dimension: str,
        filters: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if dimension not in self.ADMIN_FILTER_COLUMNS:
            raise ValueError(f"unsupported dimension: {dimension}")
        where, params = self._admin_where(project, since_day, until_day, filters)
        rows = self._query(
            f"SELECT e.{dimension} AS key, COALESCE(SUM(c.amount_usd), 0) AS cost_usd, "
            "COUNT(DISTINCT e.session_id) AS calls "
            "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
            f"WHERE {where} GROUP BY e.{dimension} ORDER BY cost_usd DESC",
            tuple(params),
        )
        return [{"key": r["key"], "calls": int(r["calls"]), "cost_usd": round(_num(r["cost_usd"]), 8)} for r in rows]

    def admin_distinct(self, project: str | None, since_day: str, column: str, limit: int = 200) -> list[str]:
        """Values seen in a filter column, for the filter dropdowns."""
        if column not in self.ADMIN_FILTER_COLUMNS:
            raise ValueError(f"unsupported column: {column}")
        where, params = self._admin_where(project, since_day, None, None)
        rows = self._query(
            f"SELECT DISTINCT e.{column} AS v FROM usage_event e WHERE {where} AND e.{column} IS NOT NULL "
            f"ORDER BY e.{column} LIMIT {int(limit)}",
            tuple(params),
        )
        return [r["v"] for r in rows]

    def admin_highlight_counts(self, project: str, since_day: str) -> dict[str, int]:
        rows = self._query(
            "SELECT day, COUNT(*) AS n FROM highlight WHERE project_id = ? AND day >= ? GROUP BY day",
            (project, since_day),
        )
        return {r["day"]: int(r["n"]) for r in rows}

    def admin_recon_runs(self, project: str | None, since_day: str) -> list[dict[str, Any]]:
        if project:
            return self.recon_runs_since(project, since_day)
        rows = self._query("SELECT * FROM recon_run WHERE day >= ? ORDER BY day DESC, provider", (since_day,))
        for row in rows:
            row["detail"] = json.loads(row.get("detail_json") or "{}")
        return rows

    def admin_sources(self, since_epoch: float, hour_start: float) -> list[dict[str, Any]]:
        """Per project and source: last event received, events in the last hour, and receive lag samples."""
        day = time.strftime("%Y-%m-%d", time.gmtime(since_epoch))
        rows = self._query(
            "SELECT project_id, source, MAX(received_epoch) AS last_received, MAX(ts_epoch) AS last_ts, "
            "SUM(CASE WHEN received_epoch >= ? THEN 1 ELSE 0 END) AS last_hour "
            "FROM usage_event WHERE day >= ? GROUP BY project_id, source ORDER BY project_id, source",
            (hour_start, day),
        )
        lags = self._query(
            "SELECT project_id, source, received_epoch - ts_epoch AS lag FROM usage_event "
            "WHERE day >= ? AND received_epoch >= ? AND received_epoch IS NOT NULL",
            (time.strftime("%Y-%m-%d", time.gmtime(hour_start)), hour_start),
        )
        by_key: dict[tuple[str, str], list[float]] = defaultdict(list)
        for r in lags:
            if r["lag"] is not None:
                by_key[(r["project_id"], r["source"])].append(max(float(r["lag"]), 0.0))
        out = []
        for r in rows:
            samples = by_key.get((r["project_id"], r["source"]), [])
            out.append(
                {
                    "project": r["project_id"],
                    "source": r["source"],
                    "last_event_epoch": float(r["last_received"] or r["last_ts"] or 0) or None,
                    "events_last_hour": int(_num(r["last_hour"])),
                    "lag_p95_s": _pct(samples, 95),
                }
            )
        return out

    # ---- cost_day rollup ------------------------------------------------------------------------
    _COST_DAY_UPSERT = (
        f"INSERT INTO cost_day ({', '.join(COST_DAY_KEY)}, cost_usd, lines, unpriced_lines, events) "
        f"VALUES ({', '.join('?' * (len(COST_DAY_KEY) + 4))}) "
        f"ON CONFLICT ({', '.join(COST_DAY_KEY)}) DO UPDATE SET cost_usd = cost_day.cost_usd + EXCLUDED.cost_usd, "
        "lines = cost_day.lines + EXCLUDED.lines, unpriced_lines = cost_day.unpriced_lines + EXCLUDED.unpriced_lines, "
        "events = cost_day.events + EXCLUDED.events"
    )

    def rebuild_cost_day(self, project: str, day: str) -> None:
        """Recompute one project-day of cost_day from current cost lines (after repricing, or a backfill)."""
        dims = ", ".join(f"COALESCE(e.{d}, '')" for d in COST_DAY_DIMS)
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(self._sql("DELETE FROM cost_day WHERE project_id = ? AND day = ?"), (project, day))
                cur.execute(
                    self._sql(
                        f"INSERT INTO cost_day ({', '.join(COST_DAY_KEY)}, cost_usd, lines, unpriced_lines, events) "
                        f"SELECT e.project_id, e.day, {dims}, c.price_source, COALESCE(SUM(c.amount_usd), 0), COUNT(*), "
                        "SUM(CASE WHEN c.price_source = 'unpriced' THEN 1 ELSE 0 END), COUNT(DISTINCT e.event_id) "
                        "FROM usage_event e JOIN cost_line c ON c.event_id = e.event_id AND c.is_current = 1 "
                        f"WHERE e.project_id = ? AND e.day = ? GROUP BY e.project_id, e.day, {dims}, c.price_source"
                    ),
                    (project, day),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def flush_cost_days(self) -> int:
        """Rebuild the days whose cost lines were superseded since the last flush. Returns days rebuilt."""
        dirty, self._dirty_cost_days = self._dirty_cost_days, set()
        for project, day in sorted(dirty):
            self.rebuild_cost_day(project, day)
        return len(dirty)

    def rebuild_cost_days(self, project: str | None = None, since_day: str | None = None) -> int:
        """Rebuild cost_day for every project-day with events (optionally one project / from a day)."""
        clauses, params = ["1 = 1"], []
        if project:
            clauses.append("project_id = ?")
            params.append(project)
        if since_day:
            clauses.append("day >= ?")
            params.append(since_day)
        pairs = self._query(
            f"SELECT DISTINCT project_id, day FROM usage_event WHERE {' AND '.join(clauses)} ORDER BY day",
            tuple(params),
        )
        for row in pairs:
            self.rebuild_cost_day(row["project_id"], row["day"])
        return len(pairs)

    def cost_day_needs_backfill(self) -> bool:
        has_events = self._query("SELECT 1 AS x FROM usage_event LIMIT 1")
        has_rollup = self._query("SELECT 1 AS x FROM cost_day LIMIT 1")
        return bool(has_events) and not has_rollup

    def _cost_day_where(
        self, project: str | None, since_day: str, until_day: str | None, filters: dict[str, str] | None
    ) -> tuple[str, list[Any]]:
        clauses, params = ["day >= ?"], [since_day]
        if until_day:
            clauses.append("day <= ?")
            params.append(until_day)
        if project:
            clauses.append("project_id = ?")
            params.append(project)
        for column, value in (filters or {}).items():
            if column not in COST_DAY_DIMS:
                raise ValueError(f"cost_day has no column {column}")
            if value in (None, ""):
                continue
            clauses.append(f"{column} = ?")
            params.append(value)
        return " AND ".join(clauses), params

    def rollup_cost_by_day(
        self, project: str | None, since_day: str, until_day: str | None, dimension: str, filters: dict[str, str]
    ) -> list[dict[str, Any]]:
        if dimension not in COST_DAY_DIMS:
            raise ValueError(f"unsupported dimension: {dimension}")
        where, params = self._cost_day_where(project, since_day, until_day, filters)
        rows = self._query(
            f"SELECT day, {dimension} AS key, COALESCE(SUM(cost_usd), 0) AS cost_usd FROM cost_day "
            f"WHERE {where} GROUP BY day, {dimension} ORDER BY day",
            tuple(params),
        )
        return [{"day": r["day"], "key": r["key"] or None, "cost_usd": round(_num(r["cost_usd"]), 8)} for r in rows]

    def rollup_cost_by(
        self, project: str | None, since_day: str, until_day: str | None, dimension: str, filters: dict[str, str]
    ) -> list[dict[str, Any]]:
        if dimension not in COST_DAY_DIMS:
            raise ValueError(f"unsupported dimension: {dimension}")
        where, params = self._cost_day_where(project, since_day, until_day, filters)
        rows = self._query(
            f"SELECT {dimension} AS key, COALESCE(SUM(cost_usd), 0) AS cost_usd FROM cost_day "
            f"WHERE {where} GROUP BY {dimension} ORDER BY cost_usd DESC",
            tuple(params),
        )
        return [{"key": r["key"] or None, "calls": None, "cost_usd": round(_num(r["cost_usd"]), 8)} for r in rows]

    def rollup_line_counts(
        self, project: str | None, since_day: str, until_day: str | None, filters: dict[str, str]
    ) -> dict[str, float]:
        where, params = self._cost_day_where(project, since_day, until_day, filters)
        rows = self._query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS cost_usd, COALESCE(SUM(lines), 0) AS lines, "
            f"COALESCE(SUM(unpriced_lines), 0) AS unpriced FROM cost_day WHERE {where}",
            tuple(params),
        )
        r = rows[0] if rows else {}
        return {"cost_usd": _num(r.get("cost_usd")), "lines": int(_num(r.get("lines"))),
                "unpriced_lines": int(_num(r.get("unpriced")))}

    def rollup_line_counts_by_day(self, project: str, since_day: str) -> dict[str, dict[str, int]]:
        rows = self._query(
            "SELECT day, COALESCE(SUM(lines), 0) AS lines, COALESCE(SUM(unpriced_lines), 0) AS unpriced "
            "FROM cost_day WHERE project_id = ? AND day >= ? GROUP BY day",
            (project, since_day),
        )
        return {r["day"]: {"lines": int(_num(r["lines"])), "unpriced_lines": int(_num(r["unpriced"]))} for r in rows}

    def rollup_distinct(self, project: str | None, since_day: str, column: str) -> list[str]:
        if column not in COST_DAY_DIMS:
            raise ValueError(f"unsupported column: {column}")
        where, params = self._cost_day_where(project, since_day, None, None)
        rows = self._query(
            f"SELECT DISTINCT {column} AS v FROM cost_day WHERE {where} AND {column} <> '' ORDER BY {column} LIMIT 200",
            tuple(params),
        )
        return [r["v"] for r in rows]

    def rollup_sessions(
        self, project: str | None, since_day: str, until_day: str | None, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        """Calls in a range from call_rollup (same shape as admin_sessions, without line counts)."""
        clauses, params = ["day >= ?"], [since_day]
        if until_day:
            clauses.append("day <= ?")
            params.append(until_day)
        if project:
            clauses.append("project_id = ?")
            params.append(project)
        if tenant:
            clauses.append("tenant_id = ?")
            params.append(tenant)
        rows = self._query(
            "SELECT project_id, session_id, day, tenant_id, minutes, cost_usd, unpriced_count FROM call_rollup "
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return [
            {
                "project": r["project_id"],
                "session_id": r["session_id"],
                "day": r["day"],
                "tenant_id": r["tenant_id"],
                "minutes": float(r["minutes"] or 0),
                "cost_usd": _num(r["cost_usd"]),
                "lines": 0,
                "unpriced_lines": int(r["unpriced_count"] or 0),
            }
            for r in rows
        ]

    # ---- client_stats ---------------------------------------------------------------------------
    def save_client_stats(self, project: str, stats: dict[str, Any], now: float, batch_events: int = 0) -> None:
        """Upsert the client's counters and count the events that arrived from it.

        `received` counts events from this client process (same `started_epoch`) that reached the collector;
        `received_before` is that count before the current batch. The client's `sent` is reported at send time
        and covers batches it saw acknowledged, so `sent` should equal `received_before`. Received above sent
        means retried batches (stored once, deduplicated); below sent means events the client saw acknowledged
        that this collector's database never counted (another collector, or a bug). Counting starts at the first
        batch seen from a process.
        """
        started = stats.get("started_epoch")
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                self._sql("SELECT received, started_epoch FROM client_stats WHERE project_id = ? AND client_id = ?"),
                (project, stats["client_id"]),
            )
            row = cur.fetchone()
            same_process = row is not None and started is not None and row[1] is not None and (
                abs(float(row[1]) - float(started)) < 0.01
            )
            tracked = same_process and int(row[0] or 0) > 0
            # First batch seen from this process (or a row from before this column existed): start counting from
            # the client's own figure, since earlier batches cannot be checked.
            before = int(row[0]) if tracked else int(stats.get("sent", 0))
            cols = ("project_id", "client_id", "sdk_version", "source", "dropped", "errors", "sent", "buffer_len",
                    "buffer_max", "started_epoch", "reported_epoch", "received", "received_before")
            values = (project, stats["client_id"], stats.get("sdk_version"), stats.get("source"),
                      int(stats.get("dropped", 0)), int(stats.get("errors", 0)), int(stats.get("sent", 0)),
                      int(stats.get("buffer_len", 0)), int(stats.get("buffer_max", 0)), started, now,
                      before + int(batch_events), before)
            sql = (
                f"INSERT INTO client_stats ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                "ON CONFLICT (project_id, client_id) DO UPDATE SET sdk_version = EXCLUDED.sdk_version, "
                "source = EXCLUDED.source, dropped = EXCLUDED.dropped, errors = EXCLUDED.errors, "
                "sent = EXCLUDED.sent, buffer_len = EXCLUDED.buffer_len, buffer_max = EXCLUDED.buffer_max, "
                "started_epoch = EXCLUDED.started_epoch, reported_epoch = EXCLUDED.reported_epoch, "
                "received = EXCLUDED.received, received_before = EXCLUDED.received_before"
            )
            cur.execute(self._sql(sql), values)
            self._conn.commit()

    def client_stats_rows(self, since_epoch: float) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM client_stats WHERE reported_epoch >= ? ORDER BY project_id, reported_epoch DESC",
            (since_epoch,),
        )
