"""LiveKit Agents adapter.

Usage:
    meter = voicetoll.livekit.attach(session, tenant="clinic_17", call_id=ctx.room.name)

    @session.on("metrics_collected")
    def _on_metrics(ev):
        meter.observe(ev.metrics)

`observe` reads a handful of attributes from LiveKit's metric objects and enqueues one event; it never
imports LiveKit and never raises. Field names follow LiveKit Agents 1.x (STTMetrics, LLMMetrics,
TTSMetrics, EOUMetrics, RealtimeModelMetrics); M2 contract tests pin them per LiveKit version.

Also importable as `voicetoll.livekit` (kept for existing apps) or `voicetoll.frameworks.load("livekit")`.
"""

from __future__ import annotations

from typing import Any

from ..client import VoiceToll
from ..providers import normalize_provider  # noqa: F401  (re-exported for voicetoll.livekit)
from ..session import CallSession
from .base import ms as _ms

NAME = "livekit"

_PLUGIN_PREFIX = "livekit.plugins."


def guess_provider(component_obj: Any) -> str | None:
    """Brand token from a LiveKit plugin object.

    LiveKit's naming convention: plugins live at `livekit.plugins.<brand>` (livekit.plugins.deepgram.STT),
    and newer plugins also expose a `provider` attribute. The token is normalized by the caller.
    """
    if component_obj is None:
        return None
    provider = getattr(component_obj, "provider", None)
    if isinstance(provider, str) and provider:
        return provider.lower()
    module = type(component_obj).__module__
    if module.startswith(_PLUGIN_PREFIX):
        return module[len(_PLUGIN_PREFIX) :].split(".")[0]
    return None


def guess_model(component_obj: Any) -> str | None:
    model = getattr(component_obj, "model", None)
    return model if isinstance(model, str) and model else None


class LiveKitMeter:
    def __init__(self, call: CallSession, fixed: set[str] | None = None) -> None:
        self.call = call
        self._speech_turns: dict[str, int] = {}
        self._fixed = set(fixed or ())  # components whose provider/model were passed explicitly
        self._pending_turn: int | None = None

    def _emit(
        self, component: str, units: Any, *, provider: Any = None, model: Any = None, **kwargs: Any
    ) -> bool:
        # Explicit `providers=` wins over what the metrics say; otherwise normalize the framework's name.
        if component in self._fixed:
            provider, model = None, None
        else:
            provider = normalize_provider(provider)
        return self.call.emit(component, units, provider=provider, model=model, **kwargs)

    def _user_turn(self, speech_id: Any) -> int:
        """A user finished speaking (end-of-utterance metrics): that always starts a new turn. The reply's
        LLM and TTS metrics may carry a speech id the EOU event did not have, so the next unseen speech id
        joins this turn instead of opening another one."""
        if speech_id and str(speech_id) in self._speech_turns:
            return self._speech_turns[str(speech_id)]  # the reply was already metered under this id
        turn = self.call.next_turn()
        self._pending_turn = turn
        if speech_id:
            self._speech_turns[str(speech_id)] = turn
            self._pending_turn = None
        return turn

    def _turn_for(self, speech_id: Any) -> int:
        if not speech_id:
            return self.call.turn
        speech_id = str(speech_id)
        turn = self._speech_turns.get(speech_id)
        if turn is None:
            if self._pending_turn is not None:
                turn, self._pending_turn = self._pending_turn, None
            else:
                turn = self.call.next_turn()
            self._speech_turns[speech_id] = turn
            if len(self._speech_turns) > 512:  # keep memory bounded on very long calls
                self._speech_turns.pop(next(iter(self._speech_turns)))
        return turn

    def observe(self, metrics: Any) -> bool:
        """Map one LiveKit metrics object to a voiceToll event. Never raises."""
        try:
            kind = getattr(metrics, "type", None) or type(metrics).__name__.lower()
            meta = getattr(metrics, "metadata", None)
            provider = getattr(meta, "model_provider", None) if meta is not None else None
            model = getattr(meta, "model_name", None) if meta is not None else None
            if kind in ("eou_metrics", "eoumetrics"):
                turn = self._user_turn(getattr(metrics, "speech_id", None))
            else:
                turn = self._turn_for(getattr(metrics, "speech_id", None))
            request_id = getattr(metrics, "request_id", None)
            g = lambda name: getattr(metrics, name, None)  # noqa: E731

            if kind in ("stt_metrics", "sttmetrics"):
                return self._emit(
                    "stt",
                    {"audio_input_seconds": g("audio_duration")},
                    src="estimated",
                    how={"audio_input_seconds": "framework_audio_duration"},
                    timing_ms={"duration": _ms(g("duration")) or None},  # streamed STT reports 0
                    provider=provider,
                    model=model,
                    turn=turn,
                    request_id=request_id,
                )
            if kind in ("llm_metrics", "llmmetrics"):
                return self._emit(
                    "llm",
                    {
                        "input_tokens": g("prompt_tokens"),
                        "output_tokens": g("completion_tokens"),
                        "cache_read_tokens": g("prompt_cached_tokens"),
                    },
                    src="reported",
                    timing_ms={"ttft": _ms(g("ttft")), "duration": _ms(g("duration"))},
                    provider=provider,
                    model=model,
                    turn=turn,
                    request_id=request_id,
                )
            if kind in ("tts_metrics", "ttsmetrics"):
                return self._emit(
                    "tts",
                    {"characters": g("characters_count"), "audio_output_seconds": g("audio_duration")},
                    src="estimated",
                    how={
                        "characters": "framework_characters_count",
                        "audio_output_seconds": "framework_audio_duration",
                    },
                    timing_ms={"ttfb": _ms(g("ttfb")), "duration": _ms(g("duration"))},
                    cancelled=bool(g("cancelled")),
                    provider=provider,
                    model=model,
                    turn=turn,
                    request_id=request_id,
                )
            if kind in ("eou_metrics", "eoumetrics"):
                return self._emit(
                    "turn",
                    None,
                    timing_ms={
                        "eou_delay": _ms(g("end_of_utterance_delay")),
                        "transcription_delay": _ms(g("transcription_delay")),
                    },
                    turn=turn,
                )
            if kind in ("realtime_model_metrics", "realtimemodelmetrics"):
                in_details = g("input_token_details")
                out_details = g("output_token_details")
                return self._emit(
                    "s2s",
                    {
                        "input_tokens": g("input_tokens"),
                        "output_tokens": g("output_tokens"),
                        "input_audio_tokens": getattr(in_details, "audio_tokens", None),
                        "cache_read_tokens": getattr(in_details, "cached_tokens", None),
                        "output_audio_tokens": getattr(out_details, "audio_tokens", None),
                    },
                    src="reported",
                    timing_ms={"ttft": _ms(g("ttft")), "duration": _ms(g("duration"))},
                    cancelled=bool(g("cancelled")),
                    provider=provider,
                    model=model,
                    turn=turn,
                    request_id=request_id,
                )
            return False  # VAD and unknown metric types are ignored
        except Exception:
            self.call.client.errors += 1
            return False


def attach(
    session: Any = None,
    *,
    tenant: str | None,
    call_id: str,
    user: str | None = None,
    feature: str | None = None,
    agent_version: str | None = None,
    providers: dict[str, tuple[str | None, str | None]] | None = None,
    client: VoiceToll | None = None,
) -> LiveKitMeter:
    """Create a meter for one LiveKit AgentSession.

    `providers` overrides detection and the provider/model named in LiveKit's metrics, e.g.
    {"tts": ("elevenlabs", "eleven_flash_v2_5")}.
    """
    components: dict[str, tuple[str | None, str | None]] = {}
    if session is not None:
        for name in ("stt", "llm", "tts"):
            obj = getattr(session, name, None)
            if obj is not None:
                components[name] = (normalize_provider(guess_provider(obj)), guess_model(obj))
        llm = getattr(session, "llm", None)
        if llm is not None and "realtime" in type(llm).__name__.lower():
            components["s2s"] = components.pop("llm")
    components.update(providers or {})
    call = CallSession(
        session_id=call_id,
        tenant=tenant,
        user=user,
        feature=feature,
        agent_version=agent_version,
        components=components,
        source=NAME,
        client=client,
    )
    return LiveKitMeter(call, fixed=set(providers or {}))
