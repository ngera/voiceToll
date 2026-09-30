# voiceToll — Speech-to-speech plan (parked, not built)

Last updated 2026-09-29 · Neeraj Gera · Status: **plan only; parked until after the admin UI**

## Summary

Speech-to-speech (S2S) models such as OpenAI Realtime, Gemini Live and Amazon Nova Sonic replace the STT → LLM → TTS chain with one model that bills audio and text tokens separately, or with a per-minute platform fee (Ultravox). voiceToll already has an `s2s` component, the audio token units and a LiveKit mapping, and voice-prices prices the main models correctly. Three things are missing (the ingestion safety fix is done): Pipecat capture, the reports and highlights that make S2S costs understandable, and reconciliation beyond OpenAI.

## What already works (checked 2026-09-29 against voice-prices HEAD b1392d1)

| Piece | State |
| --- | --- |
| Schema | `component: s2s`; units `input_tokens`, `output_tokens`, `cache_read_tokens`, `input_audio_tokens`, `output_audio_tokens`, `cache_audio_read_tokens`, `agent_minutes` |
| LiveKit capture | `RealtimeModelMetrics` → `s2s` with total input/output tokens, audio token details and cached tokens; realtime models are detected by class name |
| Pricing | Priced correctly when `input_tokens` is the **total** (text + audio + cached): `gpt-realtime`, `gpt-realtime-mini`, `gpt-4o-realtime-preview`, `gemini-live-2.5-flash-preview`, `amazon.nova-sonic-v1:0`, and Ultravox per agent minute |
| Reconciliation | OpenAI Costs API covers Realtime spend in the same OpenAI project |

Hand check, `gpt-realtime` with 1,500 input tokens (1,000 audio, 200 cached) and 1,200 output tokens (1,000 audio): 300 text in × $4/M + 200 cached × $0.40/M + 1,000 audio in × $32/M + 200 text out × $16/M + 1,000 audio out × $64/M = **$0.10048**, which is what voice-prices returns.

## Gaps

1. **An S2S event with audio tokens but no (or too small) total can stop ingestion.** voice-prices raises `ValueError: Uncached text input tokens cannot be negative` when `input_audio_tokens + cache_read_tokens > input_tokens`. `Pricer._list_price` only catches `LookupError`, so the error escapes `Pipeline.process`. `Pipeline.ingest` treats any exception as "database down" and spools the whole batch, and `Spool.replay` stops at the first failing file. Reproduced 2026-09-29: a batch with one audio-only S2S event and one valid TTS event was spooled, replay failed on every attempt, and the valid TTS event never reached the database. The file never clears, and during a real database outage every later spooled batch queues behind it. `record()` users and a future Pipecat mapping can both send audio-only counts. **Fixed 2026-09-29 (step S1 below):** pricing never raises, such events are stored as `unpriced` / `invalid_usage`, and a spool file that keeps failing with the database up moves to `spool/rejected/`.
2. **Pipecat records realtime services as text LLMs.** `component_and_provider` checks `LLM` before `Realtime`, so `OpenAIRealtimeBetaLLMService` becomes `llm` / `openairealtimebeta` and `GeminiMultimodalLiveLLMService` becomes `llm` / `geminimultimodallive`. Only prompt and completion tokens are read, so audio tokens (8× the text rate on `gpt-realtime` input) are priced as text: a large underestimate.
3. **Unit semantics aren't enforced.** voice-prices expects `input_tokens` and `output_tokens` to be totals that include audio and cached tokens. Nothing checks or derives this.
4. **LiveKit mapping is partial.** Cached audio tokens (`cache_audio_read_tokens`) and cancelled responses (billed, but cut off) are not distinguished.
5. **Nothing explains S2S costs.** The report shows one S2S line. The two things that drive S2S bills, context growth (each response re-bills the whole conversation so far) and cache hit rate, are invisible.
6. **Latency has no stage split.** There is no STT, LLM or TTS time; voice-to-voice is end of user speech to first audio byte from the model.
7. **Reconciliation is OpenAI-only.** Gemini Live bills through Google Cloud, Nova Sonic through AWS, Ultravox directly.

## Plan

| Step | Work | Done when |
| --- | --- | --- |
| S1 · Safety — **done 2026-09-29** | `Pricer` catches every exception from voice-prices and returns `unpriced` with reason `invalid_usage`; `Pipeline.process` prices before touching the store and only spools on storage errors; a poison spool file moves to `spool/rejected/` after N failed replays | A test sends audio tokens without a total and the batch is stored as unpriced, not spooled |
| S2 · Unit contract | Collector normalizes S2S token units at ingest: if `input_tokens` < audio + cached, set it to the sum and mark `how: derived_total`; same for output. Document the convention in 07 Field allow-list | Hand-calculation tests for audio-only and total-style events give the same dollars |
| S3 · LiveKit | Map cached audio tokens; keep `cancelled` on S2S events (billed, flagged); pin `RealtimeModelMetrics` fields in the contract tests | Contract test on the pinned LiveKit version |
| S4 · Pipecat | Detect `Realtime`, `MultimodalLive`, `NovaSonic` and `S2S` before `LLM`; read audio token details where the service exposes them. If Pipecat's usage metrics don't carry the audio split (to verify on a pinned version), add a small observer hook on the service's own usage events; otherwise mark the units `estimated` | Pipecat realtime fixture priced within 1% of a hand calculation |
| S5 · Per-minute S2S | For platforms billed per minute (Ultravox and similar), emit `agent_minutes` from session start to end | Ultravox fixture priced from session length |
| S6 · Reports and highlights | S2S stage in the report broken into audio in, audio out, text, cached; cost per turn line showing context growth; new highlights: **context growth** (input tokens per turn rising past a threshold; action: truncate or summarize), **low cache hit rate**, **interrupted audio** (output audio billed but cancelled, like wasted speech) | Visible on the demo agent with a realtime model |
| S7 · Latency | For S2S turns, voice-to-voice = end-of-utterance → first audio; label the stage "S2S" so latency panels don't show empty STT/TTS | Report call view shows S2S latency |
| S8 · Reconciliation | OpenAI: already covered; optionally split Realtime from text using the Costs API line items. Gemini Live: Google Cloud billing export connector. Nova Sonic: AWS Cost Explorer connector. Ultravox and others: invoice CSV import (see 09) | Each connector has a recorded fixture and contract test, like the existing three |
| S9 · Hybrid pipelines | A realtime model with text output plus a separate TTS emits both `s2s` (text tokens) and `tts` (characters); document it and test that nothing is double counted | Fixture with both components |

## Exit criteria (a G1-style gate for S2S)

- OpenAI Realtime, Gemini Live, Nova Sonic and Ultravox fixtures each within 1% of a hand calculation.
- No S2S event, however malformed, can cause a batch to spool.
- One real realtime call through the test agent, reconciled against the OpenAI Costs API for that day.

## Open questions

- [ ] Does Pipecat expose per-modality token counts for its realtime services on the version we pin, or do we need our own hook?
- [ ] Should context growth be a highlight, or a per-call metric in the report only?
- [ ] Is a Google Cloud billing export connector worth building before a design partner uses Gemini Live?
