"""voiceToll G3 test agent: a small LiveKit Agents (1.x) receptionist with voiceToll attached.

Run it daily for a week, then compare voiceToll's numbers with each provider's own usage (see README.md
in this folder). Requires the provider keys and LiveKit settings described there.

    uv run python examples/test_agent/agent.py console   # talk through your laptop mic and speakers
    uv run python examples/test_agent/agent.py dev       # join LiveKit Cloud; talk in the Agents Playground
"""

from __future__ import annotations

import logging
import os

try:  # load .env from the repo root if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from livekit.agents import Agent, AgentSession, JobContext, RunContext, WorkerOptions, cli, function_tool
# All plugins must be imported at module level: LiveKit registers plugins on import and only allows that
# on the main thread, so importing one inside the entrypoint (a job thread) fails.
from livekit.plugins import cartesia, deepgram, elevenlabs, openai, silero

import voicetoll

log = logging.getLogger("voicetoll-test-agent")

STT_MODEL = os.environ.get("VT_STT_MODEL", "nova-3")
LLM_MODEL = os.environ.get("VT_LLM_MODEL", "gpt-4o-mini")
TTS_PROVIDER = os.environ.get("VT_TTS", "elevenlabs").lower()  # elevenlabs | cartesia
TTS_MODEL = os.environ.get(
    "VT_TTS_MODEL", {"elevenlabs": "eleven_flash_v2_5", "cartesia": "sonic-3"}.get(TTS_PROVIDER, "")
)


def make_tts():
    if TTS_PROVIDER == "cartesia":
        return cartesia.TTS(model=TTS_MODEL)
    return elevenlabs.TTS(model=TTS_MODEL)


class Receptionist(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are the receptionist for Bright Smile Dental. Help callers book, move or cancel "
                "appointments. Keep every reply to one or two short sentences. Use the check_availability "
                "tool before offering times."
            )
        )

    @function_tool
    async def check_availability(self, context: RunContext, day: str) -> str:
        """Look up open appointment times for a day, e.g. 'Friday'."""
        return f"Open times on {day}: 9:30 AM, 11:00 AM and 3:15 PM."


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()

    session = AgentSession(
        stt=deepgram.STT(model=STT_MODEL),
        llm=openai.LLM(model=LLM_MODEL),
        tts=make_tts(),
        vad=silero.VAD.load(),
    )

    # voiceToll: one meter per call. Providers are passed explicitly so pricing never depends on
    # plugin attribute names. The client reads VOICETOLL_* from the environment in this job process.
    meter = voicetoll.livekit.attach(
        session,
        tenant=os.environ.get("VT_TENANT", "g3-self-test"),
        # console mode always uses the room "console-room", so add the job id to keep each call separate
        call_id=f"{ctx.room.name}-{ctx.job.id}",
        feature="reception",
        agent_version=os.environ.get("VT_AGENT_VERSION", "g3-v1"),
        providers={
            "stt": ("deepgram", STT_MODEL),
            "llm": ("openai", LLM_MODEL),
            "tts": (TTS_PROVIDER, TTS_MODEL),
        },
    )
    session.on("metrics_collected", lambda ev: meter.observe(ev.metrics))

    async def _flush_voicetoll() -> None:
        ok = voicetoll.flush(timeout=3)
        log.info("voiceToll flush ok=%s stats=%s", ok, voicetoll.stats())

    ctx.add_shutdown_callback(_flush_voicetoll)
    log.info("voiceToll call id: %s-%s", ctx.room.name, ctx.job.id)

    await session.start(room=ctx.room, agent=Receptionist())
    log.info("agent started; generating the greeting (you should hear it in a few seconds)")
    await session.generate_reply(instructions="Greet the caller and offer to help with an appointment.")
    log.info(">>> greeting finished: YOUR TURN, speak now. End the call with Ctrl+C. <<<")


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
