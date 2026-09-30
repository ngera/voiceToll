"""Minimal LiveKit Agents worker instrumented with voiceToll (for the M2/G3 end-to-end test).

Illustrative: adjust imports and plugin options to the livekit-agents version you install.
    uv pip install "livekit-agents[deepgram,openai,elevenlabs,silero]" voicetoll
    python examples/livekit_agent.py dev
Needs LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET and provider keys in the environment.
"""

from __future__ import annotations

from livekit import agents
from livekit.agents import Agent, AgentSession
from livekit.plugins import deepgram, elevenlabs, openai, silero

import voicetoll


class Receptionist(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a friendly dental clinic receptionist. Keep replies short.")


async def entrypoint(ctx: agents.JobContext) -> None:
    session = AgentSession(
        stt=deepgram.STT(model="nova-3"),
        llm=openai.LLM(model="gpt-4o-mini"),
        tts=elevenlabs.TTS(model="eleven_flash_v2_5"),
        vad=silero.VAD.load(),
    )

    meter = voicetoll.livekit.attach(
        session,
        tenant=(ctx.room.metadata or "demo-clinic"),  # use your own tenant id here
        call_id=ctx.room.name,
        feature="reception",
    )
    session.on("metrics_collected", lambda ev: meter.observe(ev.metrics))

    await session.start(agent=Receptionist(), room=ctx.room)
    await ctx.connect()


if __name__ == "__main__":
    voicetoll.configure()  # reads VOICETOLL_* from the environment
    agents.cli.run_app(agents.WorkerOptions(entrypoint_fnc=entrypoint))
