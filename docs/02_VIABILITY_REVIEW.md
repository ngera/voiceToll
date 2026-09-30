# voiceToll — Viability Review

Last updated 2026-09-27

## Verdict

The need is real but narrower than "any app that uses voice models". Frameworks already capture usage units and latency, and bundled platforms already report cost per call. The gap is turning units into **dollars across providers, per call and per customer, and checking them against the bill**. That supports a focused tool, not a broad in-process instrumentation SDK.

## 1. Is there a need?

**Evidence for**

- Voice frameworks report units, not dollars. LiveKit Agents emits `characters_count`, `audio_duration`, `ttfb`, token counts and end-of-utterance delays, and exports OpenTelemetry, but does not compute cost. Pipecat is the same: TTFB, processing time, token and character usage, no cost.
- General LLM observability tools don't cover voice cost. A Langfuse issue asking for TTS/STT cost tracking was closed as *not planned*.
- The standard doesn't cover it. OpenTelemetry's GenAI metrics define tokens, duration and time to first chunk; nothing for audio, characters or cost, and all at "Development" stability.
- True cost is confusing to buyers. Articles about Vapi pricing contrast $0.05/min advertised with $0.15–0.40/min actually paid. Someone else found the pricing gap worth building for (voice-prices).
- It maps to gross margin. Voice businesses sell by the minute, so cost per minute is their margin.

**Evidence against**

- Bundled-platform users are already covered. Vapi's call object returns `cost`, a `costBreakdown` (transport, STT, LLM, TTS, platform fee, TTS characters, token counts) and per-turn latencies.
- Framework users are partly covered: LiveKit or Pipecat metrics plus voice-prices is a short script.
- Small teams divide the monthly invoice by minutes, which is good enough for them.
- Large teams already run Datadog or Grafana and build their own.

**Who is left:** teams on custom or mixed stacks, at real volume, who need cost per customer or per call for pricing and margin decisions, e.g. a voice SaaS reselling minutes to tenants.

## 2. How companies measure it today

| Approach | Who | Shortcomings |
| --- | --- | --- |
| Monthly invoice ÷ minutes | Most small teams | No per-call, per-customer or per-feature view; late |
| Platform dashboards (Vapi, Retell) | Bundled-platform users | Locked to that platform; only as trustworthy as the platform's own accounting |
| Framework metrics → OpenTelemetry → Langfuse / Grafana / Datadog, dollars in a spreadsheet or custom code | LiveKit/Pipecat teams | Manual pricing that goes stale without notice |
| Provider dashboards and usage APIs, one per vendor | Everyone, at month end | Separate tools per vendor, daily totals only, no link to calls |
| Voice QA and observability vendors (Hamming, Cekura and similar) | Growing teams | Focus is quality, simulation and latency; a round-up checked showed no component-level dollar tracking |
| Planning calculators | Before building | Estimates, not measurements |

### Why hand-kept pricing goes stale

Someone looks up each vendor's price once and types it into a spreadsheet or config. Prices then change underneath it: vendors change rates, new models appear with no row in the table (so cost shows $0 or uses the old rate), plans change, promotions end. Nothing breaks and the dashboard keeps showing plausible numbers; the error surfaces only when someone compares against the invoice.

## 3. Implementation risks

1. **"Any app" is the hardest promise.** Provider SDKs use different transports (httpx, aiohttp, `websockets`, gRPC), serverless and edge runtimes have no background threads, and SDK updates break hooks. This is why v1 consumes framework metrics and OTLP instead of patching clients.
2. **Estimated units vs. the bill.** Rounding, minimums, character-counting rules, plan credits, committed discounts and cancelled TTS create gaps. If the tool is off by 10%, trust is gone. Nightly reconciliation is the answer.
3. **Dependence on price data.** voice-prices has one main maintainer, a commercial interest in the gateway comparison, and one plan tier per provider. Mitigation: pinned snapshots plus private rate cards.
4. **Latency measurement.** Timings taken in-process include the app's own event-loop delay; voice-to-voice latency needs VAD events only the framework sees.
5. **Zero-overhead claim needs proof.** Benchmarked in CI, not asserted.
6. **Realtime subtleties.** Speech-to-speech input tokens accumulate over the conversation, sessions drop, usage events can go missing.

## 4. Adoption risks

1. **Trust.** Patching a production HTTP client is a hard security sell; collector-side processing and an allow-listed schema reduce this.
2. **"Good enough" wins.** Framework metrics plus a spreadsheet costs nothing.
3. **Platforms absorbing the feature.** LiveKit, Pipecat or Langfuse could add dollar conversion; OpenTelemetry may standardize it. This is the most likely failure mode.
4. **Records need a home.** Solved by exporting to the team's existing dashboards instead of building one.
5. **Value appears only at volume**, and teams at volume tend to build their own.
6. **Monetization.** A cost library is hard to charge for; hosting competes with large observability vendors.

## 5. Recommendation (adopted)

Don't build the capture layer; build the part nobody owns:

1. Pricing processor over framework metrics and OTel spans (runs in the collector, zero app latency, any language).
2. Custom rate cards for negotiated rates and plan credits.
3. Invoice reconciliation against provider usage APIs, with drift alerts.
4. Unit economics per customer, call, minute and feature.
5. A thin manual `record()` SDK for custom stacks.

As a business: narrow, feature-sized, at risk of platform absorption; validate with ~10 builders first. As an open-source project and portfolio piece: strong, small and clearly scoped.

## Sources

- [LiveKit – Capturing metrics](https://docs.livekit.io/agents/ops/logging/)
- [LiveKit – usage_collector API](https://docs.livekit.io/reference/python/livekit/agents/metrics/usage_collector.html)
- [Pipecat – Metrics](https://docs.pipecat.ai/pipecat/fundamentals/metrics)
- [Langfuse issue #10276 – Logging text-to-speech](https://github.com/langfuse/langfuse/issues/10276)
- [OpenTelemetry GenAI metrics spec](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-metrics.md)
- [Vapi – Get Call API](https://docs.vapi.ai/api-reference/calls/get)
- [Vapi pricing breakdown (pxlpeak)](https://pxlpeak.com/blog/ai-tools/vapi-pricing-breakdown)
- [Cekura – Voice agent monitoring platforms](https://www.cekura.ai/blogs/voice-agent-monitoring-platforms)
- [Hamming – Voice agent observability tracing guide](https://hamming.ai/resources/voice-agent-observability-tracing-guide)
- [Softcery – Voice agent cost & latency calculator](https://softcery.com/ai-voice-agents-calculator)
