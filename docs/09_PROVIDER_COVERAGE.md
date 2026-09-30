# voiceToll — Provider coverage: does a new provider work out of the box?

Last updated 2026-09-29 · Neeraj Gera · Status: **measured; plan for review** (supersedes the first draft of this doc)

## Short answer

**Partly.** Every provider is *captured* out of the box if the framework reports metrics for it, and nothing is ever silently priced at $0. Whether it is *priced* depends on voice-prices having the model under the name the framework sends: in a sample of 47 common provider/model pairs, **29 priced correctly as sent, 9 failed to match a model, 3 matched but had no rate for the unit, and 6 speech-to-speech pairs failed on unit semantics** (they price correctly once totals are sent; see [10](10_SPEECH_TO_SPEECH.md)). Whether it is *checked against the bill* is limited to OpenAI, ElevenLabs and Deepgram. So "works" today means: dollars you can see, with gaps flagged, but only three providers verified.

## How a provider passes through voiceToll

| Layer | Provider-specific code? | Status for a new provider |
| --- | --- | --- |
| 1 · Capture (client) | No: LiveKit `metrics_collected` and Pipecat `MetricsFrame`s are the same for every plugin | ✅ Works whenever the framework emits usage. Not captured: telephony, platform fees, calls made outside the framework (use `record()`) |
| 2 · Naming (client + voice-prices) | Framework adapters reduce names to a brand token; `voicetoll.providers` maps it to the voice-prices id; voice-prices then matches provider ids by exact id or by "contains" | ✅ Mostly (below). Matters twice: for pricing, and for reconciliation, which matches the stored provider name **exactly** |
| 3 · Pricing (collector) | No: rate card → voice-prices → `unpriced` | 🟡 Depends on the model string (measured below) |
| 4 · Reconciliation (collector) | Yes: one fetcher per provider | 🔴 3 providers; others get `no_connector` |

## Measured: naming (voice-prices HEAD b1392d1, 2026-09-27)

voice-prices matches provider names loosely ("contains"), which rescues most framework spellings: `elevenlabshttp` → elevenlabs, `deepgramflux` → deepgram, `cartesiahttp` → cartesia, `geminimultimodallive` → google, `bedrock`/`amazon` → aws, `vertex` → google. **The first draft of this doc said those would go unpriced; that was wrong for pricing.** Naming still mattered because:

- **Reconciliation compares on the exact stored name.** Events stored as `elevenlabshttp` are left out of the ElevenLabs estimate, which then shows false drift. Breakdowns also split one provider into two rows.
- **Some loose matches are wrong.** `azureopenai` matched **openai**, so Azure OpenAI spend was attributed to OpenAI and would inflate OpenAI drift.
- **Some don't match at all:** `awstranscribe`, `polly`.

**Fixed on 2026-09-29** in the client refactor: each framework module owns its naming convention (LiveKit plugin module paths; Pipecat `<Brand>[Http|WebSocket]<STT|LLM|TTS>Service` class names), and `voicetoll/providers.py` maps brand tokens to voice-prices ids in one place (`azureopenai` → azure, `awstranscribe`/`polly` → aws, and so on). Tests cover the cases above.

**No provider entry in voice-prices** (priced only through a rate card): Fish Audio, PlayHT, Neuphonic, Sarvam, MiniMax, NVIDIA Riva, Resemble, Speechify, SambaNova, Baseten, Nebius, and every self-hosted model (Whisper, Kokoro, Piper, Ollama).

## Measured: pricing of common models

Model strings are the ones a LiveKit or Pipecat app typically sends; a different spelling can change the result.

| Result | Pairs |
| --- | --- |
| ✅ Priced (29) | Deepgram nova-3, nova-2, flux · ElevenLabs flash v2.5, turbo v2.5, multilingual v2 · Cartesia sonic-2, sonic-3, ink · OpenAI gpt-4o-mini, gpt-4.1-mini, gpt-4o-transcribe, whisper-1, tts-1 · Google gemini-2.5-flash, chirp 3 STT · Speechmatics enhanced · Rime mistv2 · Groq llama-3.3-70b, whisper-large-v3-turbo · Anthropic claude-sonnet-4-5 · AWS Transcribe · Azure gpt-4o-mini · LMNT blizzard · Hume octave · Gladia solaria-1 · Ultravox (per minute) · Mistral small |
| ❌ Model not found (9) | Google TTS voice names (`en-US-Chirp3-HD-…`), AssemblyAI `universal-streaming`, Rime `arcana`, AWS Polly `neural`, Azure TTS `neural`, Azure STT `standard`, Inworld `inworld-tts-1`, Cerebras `llama3.1-8b`, Twilio `voice` |
| 🟡 Model found, unit unpriced (3) | ElevenLabs `scribe_v1` (seed entry), OpenAI `gpt-4o-mini-tts` per character (it bills by audio token), Soniox `stt-rt` |
| ⚠️ S2S unit semantics (6) | OpenAI Realtime ×3, Gemini Live ×2, Nova Sonic: fail when audio tokens are sent without the total; priced correctly with totals. Also exposes an ingestion bug (see 10, step S1) |
| Freshness | Many of the most-used rates are `stale` today: Deepgram nova-3, all ElevenLabs TTS, OpenAI gpt-4o-mini, gpt-4.1-mini, whisper-1, tts-1, Gemini 2.5 Flash, Claude, Groq Llama. The freshness job now flags these; the admin Prices view will surface them |

## Why reconciliation can't be generic

A provider's bill is only reachable through that provider's own usage or billing API, each with its own auth, shape and granularity. Some report dollars (OpenAI), some only units (ElevenLabs characters, Deepgram hours), some only through a cloud billing export (Google, Azure, AWS), and some have no API at all. So "out of the box" reconciliation for every provider isn't possible; the plan below makes each connector cheap to add and gives every other provider a fallback.

## Plan to make other providers work

| Step | Work | Effect |
| --- | --- | --- |
| P1 · Naming (client) | **Done**: framework modules plus shared `voicetoll/providers.py` | Consistent names for breakdowns and reconciliation |
| P2 · Naming (collector) | `config/providers.yaml` with provider and **model** aliases applied at ingest, before pricing; store the original in `provider_raw` / `model_raw`. Covers `record()` and OTLP senders, and fixes production data with a config change plus a reprice, no app release | Fixes the 9 model misses without waiting on voice-prices (e.g. Google voice names → chirp3-hd, Polly `neural` → polly neural) |
| P3 · Catalog | Contribute the missing models and providers upstream to voice-prices; until merged, ship rate-card examples for each | Moves pairs from ❌ to ✅ for everyone |
| P4 · Unit correctness | Per-provider unit notes where the framework's unit isn't the billed unit (gpt-4o-mini-tts, streaming STT billed on wall-clock, minimum increments); add optional `min_quantity` / `increment` to rate cards | Prices match how the provider actually bills |
| P5 · Pricing conformance kit | One fixture event per provider/model with a hand-calculated amount, run in CI (G1 method); extends the new `test_provider_contracts.py` pattern from reconciliation to pricing | "Does provider X work" becomes a test result |
| P6 · Reconciliation registry | Fetchers register through an entry point group (`voicetoll.recon_fetchers`), each with recorded fixtures and a contract test using the existing `replay_fetch` / `capture_fixtures` harness | A new connector is a small package, not a core edit |
| P7 · Next connectors | In order of likely demand: Cartesia, AssemblyAI, Anthropic (Admin usage and cost API), Twilio (Usage Records), Google Cloud and AWS via billing exports, Azure Cost Management. Confirm each API before building | Moves popular providers to "Verified" |
| P8 · Fallback for everyone else | `import-invoice` CLI: a monthly CSV or invoice total per provider account compared with the month's estimate; status `unverified` shown in the admin UI where neither exists | Every provider gets some check, even monthly |
| P9 · Coverage in the admin UI | Per provider in traffic: captured, name resolved, priced by, freshness, reconciled by | The honest answer, from live data |

## Coverage tiers (to publish in the README)

| Tier | Meaning | Today |
| --- | --- | --- |
| **Verified** | Captured, priced, reconciled daily | OpenAI, ElevenLabs, Deepgram |
| **Priced** | Captured and priced from voice-prices; no bill check | The 29 ✅ pairs above and similar models |
| **Rate card** | Captured; priced once you add a rate | Providers or models voice-prices lacks; self-hosted; plan-priced accounts |
| **Manual** | Not emitted by frameworks; use `record()` or an importer | Telephony, platform fees, direct HTTP calls; Vapi via importer |

## Before claiming a provider

Run one real call through LiveKit or Pipecat with the adapter attached, confirm the admin Prices view shows no `unpriced` lines for it, and compare one day with the provider's own dashboard. Then add its fixture to the conformance kit and move it up a tier.
