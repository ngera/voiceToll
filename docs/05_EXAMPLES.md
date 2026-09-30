# voiceToll — Example Integrations

Two fictional but realistic production apps. Code is illustrative: event and class names follow current LiveKit and Pipecat docs but shift between versions, and all dollar figures are invented for the example.

## Example 1: LiveKit — "ClinicLine", AI phone receptionist for dental clinics

**Business:** SaaS used by 40 clinics; patients call to book, move and cancel appointments. ClinicLine charges clinics $0.18 per minute.

```
Phone (Twilio SIP) → LiveKit Cloud → LiveKit Agents worker (Python)
                                      ├─ STT: Deepgram nova-3
                                      ├─ LLM: OpenAI gpt-4o-mini (+ calendar tools)
                                      ├─ TTS: ElevenLabs eleven_flash_v2_5
                                      └─ VAD + turn detector
```

**Blind spot today:** bills from five vendors in different units; cost per clinic is "total ÷ minutes", so every clinic looks equally profitable.

```python
# agent.py  (LiveKit Agents worker)
from livekit.agents import AgentSession, JobContext
import voicetoll

async def entrypoint(ctx: JobContext):
    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=elevenlabs.TTS(model="eleven_flash_v2_5"),
        vad=silero.VAD.load(),
    )

    meter = voicetoll.livekit.attach(
        session,
        tenant=ctx.room.metadata["clinic_id"],   # opaque id, not a phone number
        call_id=ctx.room.name,
        feature="reception",
    )

    @session.on("metrics_collected")
    def _on_metrics(ev):
        meter.observe(ev.metrics)                 # non-blocking enqueue

    await session.start(agent=Receptionist(), room=ctx.room)
```

**Collector output (example)**

```
call lk_room_8f2a  tenant=clinic_17  duration=3m42s                        $0.4412  ($0.119/min)
 turn 1   stt 3.1s  $0.0002 | llm 612→38 tok  $0.0001 | tts 188 ch  $0.0094 | v2v 780ms
 turn 2   stt 4.7s  $0.0003 | llm 890→52 tok  $0.0002 | tts 1,420 ch $0.0710 | v2v 1.9s  ⚠
 telephony 3.7 min $0.0315 · LiveKit session $0.0370 · tts share 71%
 price data 2026-09-20 · units: tts=estimated, llm=reported, stt=estimated
```

| Finding | Shows up as | Action |
| --- | --- | --- |
| Clinic 17 loses money | $0.24/min cost vs $0.18 charged; a 1,400-character disclaimer read every booking | Shorten it, pre-record it, or reprice |
| Interrupted speech still costs | 14% of TTS characters synthesized then cut off | Smaller TTS chunks |
| New model priced at $0 | Flagged *unpriced*, not shown as $0 | Add the rate the same day |
| Cost vs latency tradeoff | Cheaper TTS: −35% cost, p95 voice-to-voice 820 → 1,150 ms | Decide with both numbers |
| Invoice check | Estimate within 1.8% of ElevenLabs usage API | Finance trusts the dashboard |

## Example 2: Pipecat — "TutorTalk", speaking-practice app

**Business:** web/mobile app; learners practise conversation with an AI tutor. $15/month unlimited; cost per active learner must stay below ~$5.

```
Browser/mobile (Daily WebRTC) → Pipecat pipeline (Python, Pipecat Cloud)
  transport.input → STT (Deepgram, streaming) → context aggregator
  → LLM (gpt-4o-mini) → TTS (Cartesia) → transport.output
```

**Blind spot today:** total monthly spend known, but not the spread across learners or lesson types.

```python
# bot.py  (Pipecat)
from pipecat.pipeline.task import PipelineTask, PipelineParams
import voicetoll

meter = voicetoll.pipecat.Observer(
    tenant="tutortalk",
    user=learner_id_hmac,                 # pseudonymized end user
    session_id=room_name,
    tags={"feature": lesson.kind},        # e.g. "roleplay_restaurant"
)

task = PipelineTask(
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[meter],                    # copies metrics frames, never blocks
)
```

| Finding | Shows up as | Action |
| --- | --- | --- |
| Silence is billed | STT is 38% of cost; 45% of streamed seconds are silence | VAD-gate the STT stream |
| Long sessions cost more per turn | LLM input tokens grow with history; turn 40 costs 6× turn 1 | Summarize context after 15 turns |
| Heavy users unprofitable | Top 4% average $21/month cost vs $15 revenue | Fair-use cap or premium tier |
| Lesson types differ | Role-play costs 2.3× grammar drills | Shorter tutor replies in role-play |
| Latency by region | EU p95 TTFB 2× US | Host a region in the EU |

## Common pattern

1. The frameworks already measure; the app writes no metering code.
2. The call path is untouched: one event handler or observer that enqueues a copy.
3. The value lands with the business: pricing and margin decisions the invoice can't show.
4. Each framework needs one mapper plus provider/model registration at session start.
