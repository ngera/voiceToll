# voiceToll — Project status

The one place for what is built, what is partly built and what is next, per function. User guides ([08 Admin UI](08_ADMIN_UI.md), [12 Getting started](12_ONBOARDING.md)) describe only what exists; design lives in [01 Architecture](01_ARCHITECTURE.md). Update this file when a function changes state.

Last updated 2026-09-30.

**States:** Shipped · Partly shipped · Planned · Parked

## Milestones and gates

| Milestone | State | Remaining |
| --- | --- | --- |
| M1 · Core pricing | Shipped | — |
| M2 · Adapters | Partly shipped | Prove G2 (enqueue under 20 µs p99) on a representative machine |
| M3 · Outputs | Partly shipped | Grafana latency and trust panels, alert rules, Prometheus scraping for fleet-wide health, weekly digest |
| M4 · Trust | Partly shipped | Pin real provider responses from G3 days; review step for catalog price changes; forgiving reconciliation (below) |
| G2 · Overhead gate | Open | Benchmark on a representative machine |
| G3 · Design-partner week | In progress | 7 days of real calls on both frameworks, each provider within 5% or explained ([11 Verification](11_VERIFICATION.md)) |

## Capture (client)

| Function | State | Notes and open items |
| --- | --- | --- |
| `record()` for any app | Shipped | |
| LiveKit Agents adapter | Shipped | Module under `voicetoll.frameworks`, contract tests on pinned 1.x |
| Pipecat adapter | Shipped | Pipecat 1.12. Open: send provider request ids (LiveKit does), so calls can be looked up by id |
| Framework registry (`voicetoll.frameworks` entry point) | Shipped | |
| Provider-name normalization (`voicetoll.providers`) | Shipped | |
| OTLP ingest | Shipped | |
| Client counters (sent, dropped, errors, buffer) | Shipped | Shown in Health with collector-side Arrived |
| `flush()` waits for in-flight batches | Shipped | Fixed 2026-09-30 |
| Early-hangup calls | Planned | Calls that end before the framework reports usage open provider connections voiceToll does not record (seen with Deepgram) |
| Speech-to-speech capture | Parked | Plan in [10 Speech to speech](10_SPEECH_TO_SPEECH.md); step S1 (ingest safety) shipped |

## Pricing (collector)

| Function | State | Notes and open items |
| --- | --- | --- |
| Rate cards, then voice-prices, else unpriced | Shipped | Unpriced lines never count as $0 |
| Rate card `reviewed` dates and review window | Shipped | |
| Repricer (CLI and automatic on price changes) | Shipped | |
| Price-freshness job | Shipped | Open: review step before a voice-prices upgrade reprices history |
| ElevenLabs cost accuracy | Open | Quota moved 55 for 500 characters in the known-input test; decide between a rate-card factor and pricing from quota units once real-call ratios are in |
| Provider coverage plan (P2–P9) | Planned | P1 (client naming) shipped; see [09 Provider coverage](09_PROVIDER_COVERAGE.md) |

## Storage and pipeline

| Function | State | Notes |
| --- | --- | --- |
| SQLite and Postgres (portable SQL) | Shipped | Postgres in CI |
| Disk spool when the database is down | Shipped | Files that keep failing with the database up move to `spool/rejected/` |
| Rollups (`call_rollup`, `tenant_day`, `user_day`, `feature_day`, `cost_day`) | Shipped | `rebuild-rollups` CLI |

## Outputs

| Function | State | Notes and open items |
| --- | --- | --- |
| Built-in report (`/report`) | Shipped | Day summary, calls, per-call turns and latency, highlights, price data, reconciliation, per-call Audit button. Open: latest drift next to cost figures in call views |
| Admin UI: Prices (in use) | Shipped | |
| Admin UI: Prices (all available) | Shipped | Catalog of every voice-prices model plus rate cards |
| Admin UI: Reports, Cost, Health, filters, saved views | Shipped | Health series are in memory, per replica |
| Admin UI: Connect providers page | Planned | Paste a key, run the `doctor` checks, save |
| Admin UI: rate card editing | Not planned for v1 | Rate cards stay YAML; the UI gives copyable snippets |
| Highlights (8 rules) | Shipped | Open: re-fire only when worse, snooze and dismiss; weekly digest |
| Grafana cost panels | Shipped | Latency and trust panels, alert rules: open |

## Checking against provider bills

| Function | State | Notes and open items |
| --- | --- | --- |
| Daily reconciliation: OpenAI, ElevenLabs, Deepgram | Shipped | Verified live on 2026-09-30 for OpenAI and ElevenLabs characters; Deepgram blocked on Deepgram's own records ([11](11_VERIFICATION.md)) |
| Setup check (`doctor`, `--write`) | Shipped | |
| Agent-key fallback (Deepgram, ElevenLabs) | Shipped | |
| Per-call audit (CLI, API, report button) | Shipped | |
| Known-input test script | Shipped | |
| Forgiving reconciliation | Planned | "Waiting for provider data" while a provider reports fewer requests than voiceToll saw; re-check the last 3 days automatically; alert only once data has settled |
| Audit by provider request id | Planned | Needs request ids from every adapter |
| ElevenLabs on the newer analytics endpoint | Planned | Character stats is deprecated upstream |
| More connectors, invoice import fallback | Planned | [09](09_PROVIDER_COVERAGE.md) P6–P8 |
| Vapi BYOK importer | Shipped | |

## Documentation

| Doc | Audience |
| --- | --- |
| [12 Getting started](12_ONBOARDING.md), [08 Admin UI](08_ADMIN_UI.md) | Users |
| [01 Architecture](01_ARCHITECTURE.md), [07 Field allow-list](07_FIELD_ALLOWLIST.md), [09](09_PROVIDER_COVERAGE.md), [10](10_SPEECH_TO_SPEECH.md) | Developers and reviewers |
| [11 Verification](11_VERIFICATION.md) | Verification runs and results |
| This file | Project status |
