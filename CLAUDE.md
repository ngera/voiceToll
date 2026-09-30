# CLAUDE.md — voiceToll

## Meta-rules for Claude

1. Propose additions to this file when a decision, convention or pattern is established; wait for explicit confirmation before editing it.
2. Source of truth for design: `docs/01_ARCHITECTURE.md` (snapshot) and the live doc linked in README. Flag conflicts between code and docs rather than silently picking one.

## What this is

voiceToll turns usage and latency events from voice frameworks (LiveKit Agents, Pipecat, OTLP, manual `record()`) into per-call, per-tenant dollars. Two packages in a uv workspace:

- `packages/voicetoll` — client, **stdlib only**, runs inside customer apps.
- `packages/voicetoll-collector` — Starlette service: ingest → scrub → price (rate cards, then voice-prices) → store (SQLite/Postgres) → summaries; disk spool when the DB is down.

## Commands

```
uv sync                                  # install
uv run pytest                            # tests (unittest-style, pytest-compatible)
uv run voicetoll-collector serve         # collector on :4319 (SQLite by default)
uv run python examples/demo_fake_call.py # simulated call end to end
uv run ruff check . && uv run ruff format .
```

## Hard rules

- **Client hot path:** `enqueue`/`observe` must never block, do I/O, raise, or add dependencies. Target: under 20 µs p99 (`tests/test_overhead.py`). Everything else happens in the exporter thread or the collector.
- **Fail open:** any exception in client code is caught and counted; the host app's call always proceeds.
- **Never capture content:** no audio, transcript text, prompts, raw URLs, headers or provider error messages. New event fields must be added to the schema allow-list deliberately (`schema.py`), never passed through.
- **Tenant/user ids are pseudonymized in the client** (`VOICETOLL_HMAC_KEY`) before they leave the app.
- **Raw units are immutable; dollars are derived.** `usage_event` is append-only. Price changes supersede `cost_line` rows (`is_current`), never edit them.
- **$0 must never mean "unknown".** Unknown model/unit → `price_source='unpriced'` with a reason; units a model doesn't bill → `not_billed`.
- **Portable SQL only** in `store.py` (runs on SQLite and Postgres): no JSON operators; dimensions are real columns.
- Framework field names (LiveKit metrics, Pipecat frames) are duck-typed; when changing them, update `tests/test_adapters.py`.

## Conventions

- Python 3.11+, type hints, `from __future__ import annotations`, line length 110.
- Unit names = voice-prices `Usage` field names (`characters`, `audio_input_seconds`, `input_tokens`, ...).
- Timings are milliseconds in events (`timing_ms`), converted from framework seconds in adapters.
- Tests must run offline; no provider keys in tests.

## Status and next steps

- M1 core pricing: done (G1 hand-calculation tests in `tests/test_pricing.py`).
- M2 adapters: mappers written and unit-tested with stand-ins; next is contract tests against pinned LiveKit/Pipecat versions, an OTLP ingest endpoint, and getting p99 enqueue under 20 µs (currently ~35 µs p99 in CI-like runs).
- M3 outputs: rollup tables, Grafana dashboards, highlights. M4: reconciliation, repricer, Vapi BYOK importer.
- Postgres path is written but not yet exercised in CI; add a Postgres service to CI.
