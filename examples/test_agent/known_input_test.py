"""Known-input test: send each provider an input whose size is fixed in advance, record it with voiceToll, and
compare three numbers per provider: what we sent, what the provider's response says, and (later) what the
provider's usage log says.

    uv run --env-file .env python examples/test_agent/known_input_test.py
    # wait 5-10 minutes for the providers' logs, then run the audit command it prints

What it sends (costs well under one cent in total):
    Deepgram    30.0 s of generated audio (a quiet tone), prerecorded, nova-3        -> 30.0 audio seconds
    ElevenLabs  exactly 500 characters of fixed English text, eleven_flash_v2_5      -> 500 characters
    OpenAI      one fixed prompt to gpt-4o-mini, max 20 output tokens               -> tokens from the response

It uses the agent keys (DEEPGRAM_API_KEY, ELEVEN_API_KEY or ELEVENLABS_API_KEY, OPENAI_API_KEY), the same ones
the test agents use, and the VOICETOLL_* settings from .env. Stdlib only. Nothing returned by the providers
(transcript, audio, reply text) is printed or kept, only sizes and counts.

Run it when nothing else is using these provider accounts, and leave a few minutes before or after other test
calls: the audit counts everything in the call's time window.
"""

from __future__ import annotations

import io
import json
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import wave

import voicetoll

AUDIO_SECONDS = 30.0
SAMPLE_RATE = 16_000
TTS_CHARACTERS = 500
STT_MODEL = "nova-3"
TTS_MODEL = "eleven_flash_v2_5"
LLM_MODEL = "gpt-4o-mini"
VOICE = os.environ.get("VT_ELEVEN_VOICE", "21m00Tcm4TlvDq8ikWAM")
PROMPT = "Reply with exactly the words: known input test complete."

# Plain text with no digits, abbreviations or symbols that a provider might expand or normalise
_SENTENCE = "The quick brown fox jumps over the lazy dog near the quiet river bank. "


def tts_text() -> str:
    text = (_SENTENCE * (TTS_CHARACTERS // len(_SENTENCE) + 1))[:TTS_CHARACTERS]
    text = text[:-1] + "."  # end on a full stop, keep the length
    assert len(text) == TTS_CHARACTERS
    return text


def wav_bytes(seconds: float) -> bytes:
    frames = int(seconds * SAMPLE_RATE)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(800 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE)))
                for i in range(frames)
            )
        )
    return buf.getvalue()


def post(url: str, body: bytes, headers: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed provider URLs
        return resp.read(), {k.lower(): v for k, v in resp.headers.items()}


def need(*names: str) -> str | None:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def run_deepgram(session: str) -> dict:
    key = need("DEEPGRAM_API_KEY")
    if not key:
        return {"provider": "deepgram", "skipped": "DEEPGRAM_API_KEY not set"}
    audio = wav_bytes(AUDIO_SECONDS)
    started = time.perf_counter()
    body, headers = post(
        f"https://api.deepgram.com/v1/listen?model={STT_MODEL}",
        audio,
        {"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
    )
    elapsed = (time.perf_counter() - started) * 1000
    meta = json.loads(body).get("metadata") or {}  # transcript is ignored
    voicetoll.record(
        "stt",
        "deepgram",
        STT_MODEL,
        {"audio_input_seconds": AUDIO_SECONDS},
        session_id=session,
        source="sdk",
        timing_ms={"duration": elapsed},
        request_id=meta.get("request_id"),
    )
    return {
        "provider": "deepgram",
        "sent": f"{AUDIO_SECONDS:.1f} s of audio",
        "response_says": f"{float(meta.get('duration') or 0):.3f} s",
        "recorded": f"{AUDIO_SECONDS:.1f} s",
    }


def run_elevenlabs(session: str) -> dict:
    key = need("ELEVEN_API_KEY", "ELEVENLABS_API_KEY")
    if not key:
        return {"provider": "elevenlabs", "skipped": "ELEVEN_API_KEY / ELEVENLABS_API_KEY not set"}
    text = tts_text()
    started = time.perf_counter()
    audio, headers = post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format=mp3_22050_32",
        json.dumps({"text": text, "model_id": TTS_MODEL}).encode(),
        {"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
    )
    elapsed = (time.perf_counter() - started) * 1000
    voicetoll.record(
        "tts",
        "elevenlabs",
        TTS_MODEL,
        {"characters": len(text)},
        session_id=session,
        source="sdk",
        timing_ms={"duration": elapsed},
        request_id=headers.get("request-id"),
    )
    charged = headers.get("character-cost") or headers.get("x-character-count")
    return {
        "provider": "elevenlabs",
        "sent": f"{len(text)} characters",
        "response_says": f"{charged} (character-cost header)" if charged else "no character-cost header",
        "recorded": f"{len(text)} characters",
        "audio_bytes": len(audio),
    }


def run_openai(session: str) -> dict:
    key = need("OPENAI_API_KEY")
    if not key:
        return {"provider": "openai", "skipped": "OPENAI_API_KEY not set"}
    started = time.perf_counter()
    body, _ = post(
        "https://api.openai.com/v1/chat/completions",
        json.dumps(
            {
                "model": LLM_MODEL,
                "max_tokens": 20,
                "temperature": 0,
                "messages": [{"role": "user", "content": PROMPT}],
            }
        ).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    elapsed = (time.perf_counter() - started) * 1000
    data = json.loads(body)  # reply text is ignored
    usage = data.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    units = {
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "cache_read_tokens": cached,
    }
    voicetoll.record(
        "llm",
        "openai",
        data.get("model") or LLM_MODEL,
        units,
        session_id=session,
        source="sdk",
        timing_ms={"duration": elapsed},
        request_id=data.get("id"),
    )
    tokens = f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out"
    return {"provider": "openai", "sent": "1 request", "response_says": tokens, "recorded": tokens}


def main() -> int:
    session = f"known-input-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    print(f"voiceToll call id: {session}\n")
    rows = []
    for run in (run_deepgram, run_elevenlabs, run_openai):
        try:
            rows.append(run(session))
        except urllib.error.HTTPError as exc:  # status only: provider error bodies are not printed
            rows.append({"provider": run.__name__.removeprefix("run_"), "error": f"HTTP {exc.code}"})
        except Exception as exc:
            rows.append({"provider": run.__name__.removeprefix("run_"), "error": type(exc).__name__})
        time.sleep(1)
    ok = voicetoll.flush(timeout=10)
    for r in rows:
        if "skipped" in r or "error" in r:
            print(f"{r['provider']:<11} {r.get('skipped') or r.get('error')}")
        else:
            print(
                f"{r['provider']:<11} sent {r['sent']:<22} provider response: {r['response_says']:<32} "
                f"voiceToll recorded: {r['recorded']}"
            )
    print(f"\nvoiceToll flush ok={ok} stats={voicetoll.stats()}")
    if not ok:
        print(
            "The events did not reach the collector. Is it running, and do VOICETOLL_ENDPOINT/INGEST_KEY match?"
        )
        return 1
    print("\nWait 5-10 minutes for the providers' usage logs, then run:\n")
    print(f"    uv run --env-file .env voicetoll-collector audit-call {session}\n")
    print(
        "or open the call in the report and press Audit against providers. Every provider should say MATCHES."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
