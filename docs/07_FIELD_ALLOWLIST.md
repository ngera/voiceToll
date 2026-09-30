# voiceToll — Field allow-list

Published list of fields that may leave the app or be stored by the collector. Anything not listed here is dropped at the adapter and again at ingest. No audio, transcript text, prompts, raw URLs, headers or provider error messages.

For security reviewers: approve this page; the ingest scrubber in `voicetoll_collector.schema` enforces it.

## Wire event (`CaptureEvent`)

| Field | Type | Notes |
| --- | --- | --- |
| `event_id` | string (8–64) | Idempotency key |
| `schema` | int | Schema version; currently `1` |
| `source` | string | e.g. `livekit`, `pipecat`, `sdk`, `otlp`, `vapi-import` |
| `ts` | ISO-8601 datetime or epoch | Event time (UTC) |
| `project` | string | Overridden by ingest key when keys are configured |
| `tenant` | string \| null | HMAC'd opaque id; never a phone/email |
| `user` | string \| null | Optional end-user id, HMAC'd |
| `session` | string | Call / room id |
| `turn` | int \| null | Turn number within the call |
| `component` | enum | `stt` \| `llm` \| `tts` \| `s2s` \| `vad` \| `telephony` \| `platform` \| `turn` |
| `provider` | string \| null | Lowercased |
| `model` | string \| null | |
| `voice_class` | string \| null | Rate-card / voice-prices voice class |
| `units` | map string → float | Keys from unit allow-list below |
| `src` | map string → `reported` \| `estimated` | Provenance per unit key |
| `how` | map string → string (≤48) | Estimation method per unit key |
| `timing_ms` | map string → float | Keys from timing allow-list; values in milliseconds |
| `status` | string | Default `ok` |
| `cancelled` | bool | e.g. interrupted TTS |
| `request_id` | string \| null | Provider request id when available |
| `tags` | map string → string | Keys from tag allow-list only |

## Unit keys

`characters`, `audio_input_seconds`, `audio_output_seconds`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `input_audio_tokens`, `output_audio_tokens`, `cache_audio_read_tokens`, `agent_minutes`, `telephony_minutes`

(Names match voice-prices `Usage` fields.)

## Timing keys

`ttfb`, `ttft`, `duration`, `eou_delay`, `transcription_delay`, `processing`

Values are milliseconds. Adapters convert framework seconds before enqueue.

## Tag keys

`feature`, `agent_version`, `env`, `region`, `caller_country`

Values are length-capped (64). At most ~200 distinct values per key per project is the operational guideline.

## Client counters (batch envelope)

Alongside `events`, a batch may carry a `client` object with the sending process's own counters. Validated by `ClientStats` in `schema.py`; unknown keys are dropped and an invalid object is ignored without failing the batch.

| Field | Type | Notes |
| --- | --- | --- |
| `client_id` | string, 8–64 chars `[A-Za-z0-9_-]` | Random per app process; not derived from the host, user or tenant |
| `sdk_version` | string ≤ 32 | voicetoll client version |
| `dropped` | int ≥ 0 | Events dropped because the in-app buffer was full or retries ran out (cumulative) |
| `errors` | int ≥ 0 | Exceptions caught in adapter code (cumulative) |
| `sent` | int ≥ 0 | Events delivered before this batch (cumulative) |
| `buffer_len`, `buffer_max` | int ≥ 0 | Buffer depth and capacity |
| `started_epoch` | float | When the client process started |

The collector adds the batch's `source` values and keeps the latest report per client (`client_stats`).

## Deliberately absent

Audio, transcript text, prompts, completions, tool arguments, raw URLs, query strings, HTTP headers, provider error bodies, free-form tags, content hashes (off by default; not in v1 schema), phone numbers or emails as tenant/user ids.
