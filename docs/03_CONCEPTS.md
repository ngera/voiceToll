# voiceToll — Concepts

Background explanations behind the design, in plain language.

## 1. How voice API costs are calculated

Most voice APIs don't bill by tokens. Each provider charges for a **unit of work**; you record units per call and multiply by a price table.

| Kind of call | Typical billing unit | Examples |
| --- | --- | --- |
| TTS (classic) | Characters of input text | ElevenLabs, Google Cloud TTS, Deepgram Aura, Cartesia |
| TTS (LLM-native) | Text tokens in, audio tokens out | OpenAI `gpt-4o-mini-tts` |
| Self-hosted or GPU-hosted | Compute time (GPU-seconds) | Replicate |
| STT | Seconds or minutes of input audio | Deepgram, Whisper API, Google STT |
| Speech-to-speech / realtime | Audio tokens in and out, plus text tokens | OpenAI Realtime, Gemini Live |

**Audio tokens** are how LLM-native voice models count audio: audio is sliced into short time segments, one token each, so token count scales with duration. Audio tokens cost much more than text tokens, and input and output are priced differently.

**Getting the count, most to least trustworthy:**

1. Reported by the provider (a `usage` object, a header, prediction metrics) → *reported*.
2. Computed from the request (character or UTF-8 byte count, per the provider's rules) → *estimated*.
3. Computed from the output (audio duration from bytes and format) → *estimated*.

**Normalizing:** convert to one comparable measure, e.g. $ per 1,000 characters, $ per audio minute, or $ per conversation minute for a full STT → LLM → TTS pipeline.

**Gotchas:** credits are not dollars; per-request minimums and rounding; retries and failed calls are often billed; cached tokens have their own price; free tiers hide list price; your estimate is not the bill.

## 2. Raw units

A raw-unit record holds everything needed to compute or recompute cost later, but not the cost itself, and never any audio or text.

| Group | Fields | Why |
| --- | --- | --- |
| Identity | provider, model, operation, endpoint template, voice class, region, plan tier | Selects the rate |
| Quantities | characters, UTF-8 bytes, audio seconds in/out, text tokens in/out, audio tokens in/out, cached tokens, requests, GPU seconds, session minutes | The billable meters |
| Provenance per quantity | reported or estimated, and how | How much to trust each number |
| Timing | timestamp, start, first byte/token, end | Price in effect at the time; latency |
| Correlation | provider request id, session, turn, tenant, tags | Roll-ups, debugging, invoice matching |

**Why store units, not dollars:** re-pricing when rates are corrected or renegotiated, invoice disputes, cheap to keep (hundreds of bytes per call), and the same records give latency and error rates.

**Metadata can still leak.** Use an allow-list, not "everything except audio":

| Leak | Guard |
| --- | --- |
| Provider error bodies quoting the input text | Store error code and class only |
| URLs and query strings carrying text or tokens | Store a path template, never the raw URL |
| Auth headers | Allow-list headers; strip everything else |
| App ids that are phone numbers or emails | HMAC per tenant, or require opaque ids |
| Cloned-voice ids | Store, but treat as sensitive in exports |
| Free-form tags | Fixed tag keys, value limits, scrubber |
| Content hashes | Off by default; salted and opt-in only |

## 3. What providers return with a response

| Kind of API | Usage info usually returned | Usually missing |
| --- | --- | --- |
| LLM | `usage` object with input/output tokens, often cached, reasoning and audio splits | Cost; plan discounts |
| Speech-to-speech / realtime | Usage per response in the final event, split text/audio/cached | Session totals (sum yourself). Input grows each turn because history is re-read |
| STT, pre-recorded | Metadata: request id, audio duration, channels, model; token-based STT returns `usage` | Billed duration after rounding |
| STT, streaming | Per-message metadata, sometimes a final summary | Reliable totals if the socket drops |
| TTS | Mostly just audio bytes plus request id and content type; some add a character or credit header | Usually no usage block; characters and duration are estimated |
| Hosted GPU | Prediction run time | The dollar rate |

Almost never returned: the dollar cost of the call. Reported coverage is strong for LLM and speech-to-speech, partial for STT, weak for TTS — so TTS, often the biggest cost line, mostly runs on estimated units.

## 4. OpenTelemetry

OpenTelemetry (OTel) is an open, vendor-neutral standard for application telemetry: traces (timed operations with attributes, nested per conversation), metrics and logs. Apps instrument once and send data over OTLP to any backend.

```
trace: call 8f2a…
└─ turn #4
   ├─ stt  provider=deepgram  model=nova-3  audio_duration=3.4s  latency=180ms
   ├─ llm  provider=openai    model=gpt-4o-mini  input_tokens=812  output_tokens=46  ttft=310ms
   └─ tts  provider=elevenlabs model=eleven_flash_v2_5  characters=212  ttfb=183ms
```

The OTel SDK's batch span processor already exports in the background, which is the "capture fast, ship later" pattern voiceToll relies on. The **Collector** receives, transforms and forwards telemetry.

**"OTel as a source"** means voiceToll consumes spans frameworks already produce and enriches them with cost (`cost.usd`, `cost.source`, `cost.price_version`, `cost.freshness`), rather than patching app code.

**Caveats:** no standard names for voice (GenAI conventions cover LLM tokens only, still in Development); sampling undercounts cost (so cost events are always sent at 100%); spans missing a model id can't be priced; only instrumented apps are covered.

## 5. What LiveKit and Pipecat do

A voice agent needs: real-time audio transport, detecting when the person stops talking, streaming STT → LLM → TTS without waiting, instant interruption handling, sub-second round trips, phone connectivity, scale, and swappable providers. A framework does that coordination — the conductor and stage crew for specialist providers.

- **LiveKit** began as real-time audio/video infrastructure and added an Agents framework, cloud hosting, telephony and its own inference gateway.
- **Pipecat** is Daily's open-source Python framework that builds agents as a pipeline of frames through processors.

Compared with hosted platforms (Vapi, Retell), frameworks give more control and lower platform fees in exchange for running it yourself. Because a framework sits in the middle of every call, it already sees every STT, LLM and TTS step with timing and usage — the natural data source for voiceToll.
