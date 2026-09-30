# voicetoll (client)

Adapters that copy voice usage and timing events from your app to a voiceToll collector. Standard library only; enqueueing an event never blocks and never raises into your code.

```python
import voicetoll

voicetoll.configure(endpoint="http://localhost:4319", project="demo", ingest_key="dev-key")

# LiveKit Agents
meter = voicetoll.livekit.attach(session, tenant="clinic_17", call_id=ctx.room.name)
session.on("metrics_collected", lambda ev: meter.observe(ev.metrics))

# Pipecat
worker = PipelineWorker(  # Pipecat 1.x; on 0.0.x the same arguments go to PipelineTask
    pipeline,
    params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
    observers=[voicetoll.pipecat.Observer(tenant="tutortalk", session_id=room)],
)

# Anything else
voicetoll.record(
    component="tts",
    provider="elevenlabs",
    model="eleven_flash_v2_5",
    units={"characters": 188},
    session_id="call-1",
    tenant="acme",
)
```

## Frameworks

The collector only sees framework-neutral capture events, so it works the same whether events come from
LiveKit, Pipecat, another framework, `record()` or OpenTelemetry spans. Framework adapters are optional
conveniences, one module each under `voicetoll.frameworks` (`livekit`, `pipecat`), and each owns its
framework's naming convention (LiveKit plugin module paths, Pipecat service class names). Provider names
are then normalized in one shared place, `voicetoll.providers`, so every adapter spells a provider the way
voice-prices and reconciliation expect.

`voicetoll.livekit` and `voicetoll.pipecat` still work and are the same modules. To add a framework,
publish a package with an entry point and follow the contract in `voicetoll/frameworks/base.py`:

```toml
[project.entry-points."voicetoll.frameworks"]
ten = "voicetoll_ten:attach"
```

```python
attach = voicetoll.frameworks.load("ten")
print(voicetoll.frameworks.available())  # livekit, pipecat, ten
```

Environment variables: `VOICETOLL_ENDPOINT`, `VOICETOLL_PROJECT`, `VOICETOLL_INGEST_KEY`, `VOICETOLL_HMAC_KEY`, `VOICETOLL_ENV`, `VOICETOLL_REGION`, `VOICETOLL_DISABLED`.
