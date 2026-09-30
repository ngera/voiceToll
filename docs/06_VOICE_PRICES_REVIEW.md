# voiceToll — Review of voice-prices

Repo: [mahimailabs/voice-prices](https://github.com/mahimailabs/voice-prices) · reviewed 2026-09-26 (latest commit 2026-09-23) · MIT licence

## What it is

A **price catalog plus cost engine**, forked from pydantic's `genai-prices` and extended for voice. It is strong on the price table and almost absent on measuring what each call used. voiceToll uses it as its price layer.

## How it works

1. **Price data.** One YAML file per provider in `prices/providers/` (about 50 providers, ~1,300 models), compiled to `data.json`. Each model has matching rules (`starts_with`, regex), rates in voice-native units, `pricing_source_url`, `prices_checked` and provenance. Supports price history, time-of-day pricing, tiers and per-voice multipliers.
   - Voice rate fields: `input_kchars` (TTS), `input_audio_kseconds` (STT), `input_audio_mtok` / `output_audio_mtok` (speech-to-speech), `agent_kminutes` (bundled platforms), `telephony_kminutes`.
2. **Cost engine.** `calc_price(Usage(...), model_ref, provider_id)` returns a `PriceCalculation` with a per-meter breakdown, plus:
   - `unpriced_usage`: units supplied that the model has no rate for — a $0 that means "no meter", not "free".
   - `freshness()`: verified / stale / imported / seed, with a confidence label from the human-checked date.
3. **Usage extraction.** `extract_usage(response_json, provider_id=...)` maps JSON paths to `Usage` fields — **only for LLM-style providers**. ElevenLabs, Deepgram, Cartesia, AssemblyAI have no extractor; you count characters or seconds yourself.
4. **Keeping prices current.** `UpdatePrices` refreshes `data.json` from GitHub in the background; a manually triggered GitHub Action re-reads vendor pricing pages with a headless browser and an LLM extractor and opens a PR for human review.

Its own angle is comparing direct vs gateway (LiveKit Inference) prices; it discloses it is maintained alongside a commercial product (VoiceGateway).

## Fit against voiceToll's needs

| voiceToll component | voice-prices | Notes |
| --- | --- | --- |
| Pricing registry | ✅ Strong | History, provenance, freshness, source URLs |
| Normalized usage schema | ✅ | `Usage` covers characters, audio seconds, audio tokens, agent and telephony minutes; summable |
| Detector | 🟡 | Strong model matching; `api_pattern` exists but nothing intercepts traffic |
| Voice meters | 🔴 | LLM extractors only |
| Reported vs estimated flag | 🔴 | `unpriced_usage` is related but different |
| Integration hooks | 🔴 | Library only |
| Streaming / realtime | 🔴 | Out of scope |
| Sinks, aggregation, budgets | 🔴 | Out of scope |
| Price version per record | 🟡 | Request timestamp selects historical prices |
| Invoice reconciliation | 🔴 | |

**Data limits:** ElevenLabs is priced at the published API rate, not a plan's effective credit cost; one plan tier per provider file; no GPU-second unit; some providers (e.g. Speechify) absent.

**Risks:** young, largely one maintainer; schema has moved (`audio_output_seconds` changed type in 0.0.7); commercial interest in the gateway comparison. Mitigated by pinning a version and layering private rate cards.

**Opportunity:** voice usage mappers are its biggest gap and could be contributed upstream.
