# voiceToll — Voice Agent Framework Landscape

As of September 2026. Star counts are approximate and change quickly. How much a group needs voiceToll depends on who pays the providers and who does the cost math.

## 1. Open-source frameworks (you host; you pay each provider directly)

| Framework | Backer | Language | Traction | Known for | Needs voiceToll? |
| --- | --- | --- | --- | --- | --- |
| LiveKit Agents | LiveKit | Python, Node | ~13k ★ | Real-time media infra, telephony, video, cloud, inference gateway | **High.** Usage + latency, no dollars; direct vs gateway pricing side by side |
| Pipecat | Daily | Python | ~14k ★ | Flexible pipeline, many providers and carriers, Pipecat Cloud | **High.** TTFB and token/character usage, no dollars |
| TEN Framework | Agora | Python, Go, C++, Node | ~11k ★ | Graph runtime across languages, strong in Asia | High, but not a v1 adapter (OTLP or `record()` instead) |
| Vision Agents | Stream | Python | ~8k ★ | Voice plus video, provider-agnostic | Medium-high |
| Dograh | Community | Python | ~5k ★ | Self-hosted platform with dashboard | Medium |
| Bolna | Bolna | Python | smaller | Indian-language telephony | Medium |
| Jambonz | FirstFive8 | Node | infra | Open-source SIP plumbing under Vapi and Retell | Low directly |
| Vocode | YC, acquired | Python | ~3.8k ★ | Unmaintained since 2024 | Skip |

## 2. Vendor SDKs

| SDK | Positioning | Needs voiceToll? |
| --- | --- | --- |
| OpenAI Agents SDK (Realtime / voice) | ~28k ★ (Python), native phone support, built-in tracing | Medium: usage tokens per response, dollars left to you |
| Gemini Live / ADK, AWS Nova Sonic, Azure Voice Live | Speech-to-speech per cloud | Low–medium: cloud billing tools exist; gap is per-call attribution |

## 3. Managed platforms (they run everything, bill per minute)

| Platform | Billing | Needs voiceToll? |
| --- | --- | --- |
| Vapi | $0.05/min platform fee plus providers; bring-your-own-keys | **Medium, specific gap:** with BYOK, provider costs land on your own accounts and show as $0 in Vapi's `costBreakdown` |
| Retell AI | Component-billed, ~$0.07–0.31/min | Low: per-call cost reported |
| ElevenLabs Agents, Deepgram Voice Agent API, Ultravox, Cartesia Line, Telnyx Voice AI | Bundled per-minute | Low: one vendor, one bill |
| Bland, Synthflow, NiCE Cognigy | Fully bundled or enterprise | Low |

## Who needs it most

1. Teams on LiveKit or Pipecat (or TEN) with several providers.
2. Vapi users bringing their own keys.
3. Teams running more than one stack, needing one comparable $/minute.
4. Voice SaaS reselling minutes to tenants.

Least need: single bundled-platform users, where the invoice is the answer.

## Sources

- [Micdrop – Best open-source voice agent frameworks 2026](https://micdrop.dev/blog/open-source-voice-agent-frameworks)
- [Softcery – 12 voice agent platforms compared (2026)](https://softcery.com/lab/choosing-the-right-voice-agent-platform-in-2026)
- [LiveKit – Best open-source frameworks for realtime voice and video agents](https://livekit.com/blog/best-open-source-voice-and-video-ai-agent-frameworks)
- [Forasoft – Pipecat vs LiveKit vs OpenAI](https://www.forasoft.com/blog/article/pipecat-vs-livekit-agents)
- [RoomKit – Pipecat, TEN, LiveKit Agents compared](https://www.roomkit.live/blog/choosing-the-right-conversational-ai-framework/)
- [ThinnestAI – Pipecat vs LiveKit vs Bolna](https://www.thinnest.ai/blog/open-source-voice-ai-frameworks)
- [OpenAI – Agents SDK (Python)](https://github.com/openai/openai-agents-python)
- [Vapi – Get Call API](https://docs.vapi.ai/api-reference/calls/get)
- [voice-prices – Vapi pricing note](https://github.com/mahimailabs/voice-prices/blob/main/prices/providers/vapi.yml)
