# voiceToll — Verifying the numbers

How to confirm that voiceToll's per-call units and dollars match what each provider logged, without deleting local data. Results of each run are at the end.

## Why daily reconciliation was not enough

Reconciliation compares whole UTC days per provider account. On 2026-09-29 it showed Deepgram 26% below the provider and ElevenLabs 2.5× above it, but a day mixes every run (including older agent versions) and the providers' figures may be in different units (ElevenLabs credits vs characters). Deleting local data would not help, because the providers keep theirs. The checks below narrow the comparison to one call and to inputs whose size is known in advance.

## The four checks

| Check | What it proves | How to run |
| --- | --- | --- |
| Known-input test | Each unit voiceToll records equals what was sent and what the provider says it received: 30.0 s of audio to Deepgram, exactly 500 characters to ElevenLabs (`eleven_flash_v2_5`), one fixed prompt to OpenAI | `uv run --env-file .env python examples/test_agent/known_input_test.py` |
| Per-call audit | For one call, voiceToll's units (and Deepgram dollars) against each provider's per-request log for that call's minutes | `uv run --env-file .env voicetoll-collector audit-call <call-id>`, or **Audit** next to a call in `/report`, or `GET /v1/audit/{call}` |
| Sent vs Arrived | No events are lost between the app and the collector | Admin → Health → Clients: Sent (client) should equal Arrived (collector) |
| Daily reconciliation | Whole-day totals per account, once calls are verified | `voicetoll-collector reconcile --day YYYY-MM-DD` (automatic for yesterday) |

## What the audit reads

| Provider | Endpoint | Compared on |
| --- | --- | --- |
| ElevenLabs | `GET /v1/history` (window by `date_after_unix`/`date_before_unix`) | Characters of text (length measured, text dropped); quota moved shown alongside |
| Deepgram | `GET /v1/projects/{id}/requests` (`start`/`end`, `endpoint=listen`) | Audio seconds and dollars per request |
| OpenAI | `GET /v1/organization/usage/completions`, 1-minute buckets, filtered to the configured project | Input and output tokens (both gross of cached tokens) |

Window: 2 minutes before the call's first event to 1 minute after its last (`--pad-before`, `--pad-after`). Result per provider: `ok` (within 5%), `drift`, `no_provider_data` (logs can lag 5–10 minutes), `fetch_failed`, `skipped_no_key`, `no_account`, `no_connector`. Anything else on the same provider accounts inside the window is counted, so test calls need a few minutes between them.

## Test sequence

1. Start the collector with `uv run --env-file .env voicetoll-collector serve`.
2. Run the known-input test. Note the call id it prints.
3. A few minutes later, make one LiveKit call (about 1 minute). Hang up and note the call id from the agent log.
4. A few minutes later, make one Pipecat call (about 1 minute). Note the call id.
5. Wait 10 minutes, then run `audit-call` for all three and check Admin → Health → Clients.

## Reading the results

| Result | Likely meaning | Next step |
| --- | --- | --- |
| Known-input all `ok` | Recording and pricing are right; any gap in real calls comes from the adapters | Compare LiveKit and Pipecat audits |
| ElevenLabs characters match, quota differs | The dashboard/daily figure is in credits (for example half a credit per character on Flash) | Switch ElevenLabs reconciliation to history-based characters |
| Deepgram seconds low on real calls only | Streaming STT bills the whole open stream (including silence); the adapter counts speech only, or end-of-call metrics are lost | Fix the adapter: measure stream duration |
| Deepgram shows more requests than calls | Reconnects or several streams per call | Count streams per call in the adapter |
| OpenAI tokens differ | Tool-call or cached tokens counted differently | Check gross vs net input in the adapter |
| Sent above Arrived | Events acknowledged but not counted by this collector | Check endpoint configuration, then the spool |

## Results

### 2026-09-30 (known-input test, one Pipecat call, one LiveKit call)

| Provider | Result |
| --- | --- |
| OpenAI | **Verified.** Exact token match on all three: known-input 18/5, Pipecat 3,257/264, LiveKit 4,797/400. Daily dollars match too ($0.0025317 both sides) |
| ElevenLabs characters | **Verified.** Known-input 500 vs 500 in `/v1/history`; the day so far 3,377 vs 3,362 in character stats (0.4%) |
| ElevenLabs cost | **Open.** For the 500-character request the account quota moved by 55 (the `character-cost` header also said 55). voiceToll prices characters × list rate, so ElevenLabs dollars are overstated for this account. Need the ratio on real calls to decide between a rate-card factor and pricing from quota units |
| Deepgram | **Blocked on Deepgram.** Both keys belong to project voicetoll-g3. Its request log and daily usage show only 3 requests today (the Pipecat attempts at 15:55, 15:57 and 16:00, 89 s), none of the 6 made. Looking up the known-input request (`01a0f36f-…`, id timestamp 17:49:10) and the LiveKit stream (`01a0f38d-…`, 18:22:42) by id returns 200 with `null`. voiceToll has 364 s for the day. Re-check later with the same lookup; raise with Deepgram support if still missing |
| Deepgram, early-hangup calls | **Gap found.** The 15:55 and 15:57 Pipecat attempts opened Deepgram connections (logged by Deepgram) but voiceToll recorded no STT usage for them: calls that end before the framework emits usage are under-counted |

Also fixed during the run: `flush()` could return before a batch the background thread was sending had been acknowledged (seen as `sent: 2` for 3 events); it now waits for in-flight batches.

Follow-ups: Pipecat STT events carry no Deepgram request id (LiveKit's do), so Pipecat calls cannot be looked up by id; record STT connection time for calls that end early; decide how to price ElevenLabs once real-call ratios are in.
