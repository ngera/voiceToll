"""voiceToll G3 test agent, Pipecat edition: the same dental receptionist as agent.py (LiveKit), built on
Pipecat 1.x with voiceToll's observer attached.

Same stack as the LiveKit agent so the two can be compared call for call: Deepgram STT, OpenAI LLM with one
tool, ElevenLabs or Cartesia TTS, Silero VAD. See README.md in this folder for keys and the daily check.

    uv run --group test-agent-pipecat python examples/test_agent/pipecat_agent.py          # browser: http://localhost:7860
    uv run --group test-agent-pipecat python examples/test_agent/pipecat_agent.py local    # laptop mic and speakers
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

try:  # load .env from the repo root if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import voicetoll
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

STT_MODEL = os.environ.get("VT_STT_MODEL", "nova-3")
LLM_MODEL = os.environ.get("VT_LLM_MODEL", "gpt-4o-mini")
TTS_PROVIDER = os.environ.get("VT_TTS", "elevenlabs").lower()  # elevenlabs | cartesia
TTS_MODEL = os.environ.get(
    "VT_TTS_MODEL", {"elevenlabs": "eleven_flash_v2_5", "cartesia": "sonic-3"}.get(TTS_PROVIDER, "")
)
# Voices: any voice id from your ElevenLabs or Cartesia library. The defaults are stock voices.
ELEVEN_VOICE = os.environ.get("VT_ELEVEN_VOICE", "21m00Tcm4TlvDq8ikWAM")
CARTESIA_VOICE = os.environ.get("VT_CARTESIA_VOICE", "86e30c1d-714b-4074-a1f2-1cb6b552fb49")
AGENT_VERSION = os.environ.get("VT_AGENT_VERSION_PIPECAT", "g3-pipecat-v1")

INSTRUCTIONS = (
    "You are the receptionist for Bright Smile Dental. Help callers book, move or cancel appointments. "
    "Keep every reply to one or two short sentences, with no lists or formatting, because it is spoken aloud. "
    "Use the check_availability tool before offering times."
)


async def check_availability(params: FunctionCallParams, day: str):
    """Look up open appointment times for a day.

    Args:
        day: The day to check, e.g. "Friday".
    """
    await params.result_callback({"day": day, "open_times": ["9:30 AM", "11:00 AM", "3:15 PM"]})


def make_tts():
    if TTS_PROVIDER == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService

        return CartesiaTTSService(
            api_key=os.environ["CARTESIA_API_KEY"],
            settings=CartesiaTTSService.Settings(model=TTS_MODEL, voice=CARTESIA_VOICE),
        )
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

    # LiveKit's plugin reads ELEVEN_API_KEY; Pipecat's examples use ELEVENLABS_API_KEY. Either works here.
    key = os.environ.get("ELEVENLABS_API_KEY") or os.environ["ELEVEN_API_KEY"]
    return ElevenLabsTTSService(
        api_key=key, settings=ElevenLabsTTSService.Settings(model=TTS_MODEL, voice=ELEVEN_VOICE)
    )


def make_worker(transport: BaseTransport, call_id: str) -> tuple[PipelineWorker, LLMContext]:
    stt = DeepgramSTTService(
        api_key=os.environ["DEEPGRAM_API_KEY"], settings=DeepgramSTTService.Settings(model=STT_MODEL)
    )
    llm = OpenAILLMService(
        api_key=os.environ["OPENAI_API_KEY"],
        settings=OpenAILLMService.Settings(model=LLM_MODEL, system_instruction=INSTRUCTIONS),
    )
    tts = make_tts()

    context = LLMContext(tools=[check_availability])
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
    )
    pipeline = Pipeline(
        [transport.input(), stt, user_aggregator, llm, tts, transport.output(), assistant_aggregator]
    )

    # voiceToll: one observer per call. Providers are passed explicitly so pricing never depends on
    # Pipecat class names. enable_metrics and enable_usage_metrics make Pipecat emit the numbers it reads.
    observer = voicetoll.pipecat.Observer(
        tenant=os.environ.get("VT_TENANT", "g3-self-test"),
        session_id=call_id,
        feature="reception",
        agent_version=AGENT_VERSION,
        providers={
            "stt": ("deepgram", STT_MODEL),
            "llm": ("openai", LLM_MODEL),
            "tts": (TTS_PROVIDER, TTS_MODEL),
        },
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[observer],
    )
    return worker, context


def greet(context: LLMContext) -> None:
    context.add_message(
        {"role": "developer", "content": "Greet the caller and offer to help with an appointment."}
    )


def flush_voicetoll() -> None:
    ok = voicetoll.flush(timeout=3)
    logger.info(f"voiceToll flush ok={ok} stats={voicetoll.stats()}")


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    call_id = f"pipecat-{getattr(runner_args, 'session_id', None) or uuid.uuid4().hex[:12]}"
    logger.info(f"voiceToll call id: {call_id}")
    worker, context = make_worker(transport, call_id)
    runner = WorkerRunner(handle_sigint=runner_args.handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        greet(context)
        await worker.queue_frames([LLMRunFrame()])
        logger.info(">>> greeting queued: speak when it finishes. Close the browser tab to end the call. <<<")

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await runner.cancel()

    try:
        await runner.run()
    finally:
        flush_voicetoll()


async def bot(runner_args: RunnerArguments) -> None:
    """Entry point for Pipecat's development runner (browser via SmallWebRTC, or Daily with -t daily)."""
    from pipecat.runner.utils import create_transport

    def daily_params():
        from pipecat.transports.daily.transport import DailyParams  # only with pipecat-ai[daily]

        return DailyParams(audio_in_enabled=True, audio_out_enabled=True)

    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        "daily": daily_params,
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


async def run_local() -> None:
    """Laptop mic and speakers, no browser (needs PyAudio). Use headphones so the agent does not hear itself."""
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    transport = LocalAudioTransport(LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True))
    call_id = f"pipecat-local-{uuid.uuid4().hex[:12]}"
    logger.info(f"voiceToll call id: {call_id}")
    worker, context = make_worker(transport, call_id)
    runner = WorkerRunner()
    await runner.add_workers(worker)
    greet(context)
    await worker.queue_frames([LLMRunFrame()])
    logger.info(">>> speak after the greeting. End the call with Ctrl+C. <<<")
    try:
        await runner.run()
    finally:
        flush_voicetoll()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "local":
        asyncio.run(run_local())
    else:
        from pipecat.runner.run import main

        main()
