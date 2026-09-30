"""Send a simulated three-turn LiveKit call to a running collector and print what it costs.

No voice agent or provider keys needed:
    uv run voicetoll-collector serve            # terminal 1
    uv run python examples/demo_fake_call.py    # terminal 2
"""

from __future__ import annotations

import json
import os
import types
import urllib.request
import uuid

import voicetoll

ENDPOINT = os.environ.get("VOICETOLL_ENDPOINT", "http://localhost:4319")
KEY = os.environ.get("VOICETOLL_INGEST_KEY", "dev-key")


def m(**kw):
    return types.SimpleNamespace(**kw)


def main() -> None:
    voicetoll.configure(endpoint=ENDPOINT, ingest_key=KEY, project="demo",
                        hmac_key=os.environ.get("VOICETOLL_HMAC_KEY", "demo-secret"), env="dev")
    call_id = f"demo-{uuid.uuid4().hex[:8]}"
    meter = voicetoll.livekit.attach(
        None, tenant="clinic_17", call_id=call_id, feature="reschedule_appointment",
        agent_version="receptionist-v14",
        providers={"stt": ("deepgram", "nova-3"), "llm": ("openai", "gpt-4o-mini"),
                   "tts": ("elevenlabs", "eleven_flash_v2_5")},
    )
    turns = [  # (speech seconds, prompt tokens, completion tokens, TTS characters, TTS ttfb s)
        (3.1, 612, 38, 188, 0.18),
        (4.7, 890, 52, 1420, 0.31),  # long insurance disclaimer
        (2.2, 1105, 21, 96, 0.17),
    ]
    for i, (speech, p_tok, c_tok, chars, ttfb) in enumerate(turns, start=1):
        sid = f"speech_{i}"
        meter.observe(m(type="eou_metrics", end_of_utterance_delay=0.42, transcription_delay=0.19, speech_id=sid))
        meter.observe(m(type="stt_metrics", audio_duration=speech, duration=0.0))
        meter.observe(m(type="llm_metrics", prompt_tokens=p_tok, completion_tokens=c_tok, prompt_cached_tokens=0,
                        ttft=0.31, duration=0.7, speech_id=sid))
        meter.observe(m(type="tts_metrics", characters_count=chars, audio_duration=chars / 16, ttfb=ttfb,
                        duration=0.9, cancelled=False, speech_id=sid))
    ok = voicetoll.flush(timeout=5)
    print(f"sent: {ok}  stats: {voicetoll.stats()}")

    req = urllib.request.Request(f"{ENDPOINT}/v1/sessions/{call_id}", headers={"X-Voicetoll-Key": KEY})
    with urllib.request.urlopen(req, timeout=5) as resp:
        summary = json.load(resp)
    print(json.dumps({k: summary[k] for k in ("session_id", "turns", "cost_usd", "components", "cost_by_turn",
                                              "latency_ms")}, indent=2))


if __name__ == "__main__":
    main()
