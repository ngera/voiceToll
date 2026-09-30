"""Pipecat adapter: an observer that reads metrics frames as they pass.

Usage (Pipecat 1.x; on 0.0.x pass the same arguments to PipelineTask):
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[voicetoll.pipecat.Observer(tenant="tutortalk", session_id=room_name)],
    )

The observer never modifies or delays frames. Frames are recognised by class name, so importing this
module does not import Pipecat. Observers see a frame once per hop between processors, so frames are
de-duplicated by id. Field names follow Pipecat's metrics module (TTFBMetricsData, ProcessingMetricsData,
LLMUsageMetricsData, TTSUsageMetricsData, and STTUsageMetricsData since Pipecat 1.x); M2 contract tests pin
them per Pipecat version.

STT audio seconds: when the STT service reports STTUsageMetricsData (Pipecat 1.x, most streaming services),
those seconds are used, since they approximate the stream duration providers bill. Otherwise the observer
counts the PCM bytes it sees going in. With a Pipecat that has STT usage metrics, the PCM count is held back
and only emitted at the end of the call if the service never reported usage, so the two never double count.

LLM input tokens: Pipecat services report `prompt_tokens` either gross (OpenAI-compatible) or net of the
prompt cache (Anthropic, Bedrock); `total_tokens` is gross either way. voice-prices expects gross input, so
input = total_tokens - completion_tokens when total_tokens is present.

Also importable as `voicetoll.pipecat` (kept for existing apps) or `voicetoll.frameworks.load("pipecat")`.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any

from ..client import VoiceToll
from ..providers import normalize_provider
from ..session import CallSession

NAME = "pipecat"

# Pipecat's naming convention: services are classes named <Brand>[<Variant>]<STT|LLM|TTS>Service, and
# processors are reported as "<ClassName>#<n>". The component comes from the marker; the brand is what
# precedes it, minus transport variants. Marker order is unchanged for now: realtime services
# ("OpenAIRealtimeBetaLLMService") still match "LLM" first; see docs/10_SPEECH_TO_SPEECH.md.
_COMPONENT_MARKERS = (("STT", "stt"), ("LLM", "llm"), ("TTS", "tts"), ("Realtime", "s2s"), ("S2S", "s2s"))
# Transport or API variants that are not part of the brand: ElevenLabsHttpTTSService, CartesiaHttpTTSService.
_VARIANT_SUFFIX = re.compile(r"(Http|HTTP|WebSocket|Websocket|WS|Streaming|Stream)$")
# Brand prefixes whose Pipecat spelling carries a product name (checked against voice-prices ids by the
# shared aliases in voicetoll.providers where the product maps to a different provider).
_BRAND_PREFIXES = {
    "DeepgramFlux": "deepgram",
    "AWSTranscribe": "aws",
    "AWSPolly": "aws",
    "Polly": "aws",
    "AzureOpenAI": "azure",
}
_SEEN_LIMIT = 4096


def brand_token(prefix: str) -> str | None:
    """'ElevenLabsHttp' -> 'elevenlabs'; 'DeepgramFlux' -> 'deepgram'; 'OpenAI' -> 'openai'."""
    while True:
        stripped = _VARIANT_SUFFIX.sub("", prefix)
        if stripped == prefix or not stripped:
            break
        prefix = stripped
    if not prefix:
        return None
    return normalize_provider(_BRAND_PREFIXES.get(prefix, prefix))


def component_and_provider(processor_name: str) -> tuple[str | None, str | None]:
    """'DeepgramSTTService#0' -> ('stt', 'deepgram'); 'ElevenLabsHttpTTSService' -> ('tts', 'elevenlabs')."""
    name = processor_name.split("#")[0]
    for marker, component in _COMPONENT_MARKERS:
        idx = name.find(marker)
        if idx > 0:
            return component, brand_token(name[:idx])
        if idx == 0:
            return component, None
    return None, None


class _Core:
    """Framework-free logic, shared by the Pipecat-derived class and tests."""

    def __init__(
        self,
        *,
        session_id: str,
        tenant: str | None = None,
        user: str | None = None,
        feature: str | None = None,
        agent_version: str | None = None,
        tags: dict[str, str] | None = None,
        providers: dict[str, tuple[str | None, str | None]] | None = None,
        count_input_audio: bool = True,
        client: VoiceToll | None = None,
    ) -> None:
        self.call = CallSession(
            session_id=session_id,
            tenant=tenant,
            user=user,
            feature=(tags or {}).get("feature", feature),
            agent_version=(tags or {}).get("agent_version", agent_version),
            components=dict(providers or {}),
            source=NAME,
            client=client,
        )
        self._providers_fixed = set((providers or {}).keys())
        self._count_audio = count_input_audio
        self._seen: OrderedDict[Any, None] = OrderedDict()
        self._audio_bytes_per_second: float | None = None
        self._audio_bytes = 0
        self._ttfb: dict[str, float] = {}
        self._processing: dict[str, float] = {}
        self._interruptions = 0
        # STT usage metrics exist in this Pipecat: hold PCM-based seconds back as an end-of-call fallback only
        self._stt_usage_available = _pipecat_has_stt_usage()
        self._stt_usage_seen = False
        self._pcm_pending_seconds = 0.0

    # ---- helpers ---------------------------------------------------------------------------
    def _first_time(self, frame: Any) -> bool:
        key = getattr(frame, "id", None)
        if key is None:
            key = id(frame)
        if key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return True

    def _register(self, processor: str, model: str | None) -> str | None:
        component, provider = component_and_provider(processor)
        if component is None:
            return None
        if component not in self._providers_fixed:
            old_provider, old_model = self.call.components.get(component, (None, None))
            self.call.set_component(component, provider or old_provider, model or old_model)
        return component

    # ---- frame handling --------------------------------------------------------------------
    def handle_frame(self, frame: Any) -> None:
        try:
            if not self._first_time(frame):
                return
            kind = type(frame).__name__
            if kind == "MetricsFrame":
                for item in getattr(frame, "data", None) or []:
                    self._handle_metric(item)
            elif kind == "InputAudioRawFrame" and self._count_audio:
                audio = getattr(frame, "audio", b"") or b""
                rate = getattr(frame, "sample_rate", None) or 16000
                channels = getattr(frame, "num_channels", None) or 1
                self._audio_bytes_per_second = float(rate) * float(channels) * 2.0  # 16-bit PCM
                self._audio_bytes += len(audio)
            elif kind == "UserStoppedSpeakingFrame":
                self._end_user_turn()
            elif kind in ("StartInterruptionFrame", "InterruptionFrame"):
                self._interruptions += 1
            elif kind in ("EndFrame", "CancelFrame"):
                self._end_user_turn()
                self._end_call()
        except Exception:
            self.call.client.errors += 1

    def _handle_metric(self, item: Any) -> None:
        kind = type(item).__name__
        processor = str(getattr(item, "processor", "") or "")
        model = getattr(item, "model", None)
        component = self._register(processor, model)
        if component is None:
            return
        value = getattr(item, "value", None)
        if kind == "TTFBMetricsData" and value is not None:
            self._ttfb[component] = float(value) * 1000.0
        elif kind == "ProcessingMetricsData" and value is not None:
            self._processing[component] = float(value) * 1000.0
        elif kind == "STTUsageMetricsData" and value is not None:
            self._stt_usage_seen = True
            self.call.emit(
                component,
                {"audio_input_seconds": getattr(value, "audio_seconds", None)},
                src="estimated",
                how={"audio_input_seconds": "pipecat_stt_usage"},
                timing_ms={
                    "ttfb": self._ttfb.pop(component, None),
                    "processing": self._processing.pop(component, None),
                },
            )
        elif kind == "LLMUsageMetricsData" and value is not None:
            prompt = getattr(value, "prompt_tokens", None)
            completion = getattr(value, "completion_tokens", None)
            total = getattr(value, "total_tokens", None)
            gross_input = prompt
            if isinstance(total, int | float) and isinstance(completion, int | float) and total >= completion:
                gross_input = max(total - completion, prompt or 0)  # gross of the prompt cache
            self.call.emit(
                component,
                {
                    "input_tokens": gross_input,
                    "output_tokens": completion,
                    "cache_read_tokens": getattr(value, "cache_read_input_tokens", None),
                    "cache_write_tokens": getattr(value, "cache_creation_input_tokens", None),
                },
                src="reported",
                timing_ms={
                    "ttft": self._ttfb.pop(component, None),
                    "processing": self._processing.pop(component, None),
                },
            )
        elif kind == "TTSUsageMetricsData" and value is not None:
            self.call.emit(
                component,
                {"characters": int(value)},
                src="estimated",
                how={"characters": "framework_characters"},
                timing_ms={
                    "ttfb": self._ttfb.pop(component, None),
                    "processing": self._processing.pop(component, None),
                },
                cancelled=self._interruptions > 0,
            )
            self._interruptions = 0

    def _end_user_turn(self) -> None:
        if (
            self._audio_bytes
            and self._audio_bytes_per_second
            and (self._stt_usage_available or self._stt_usage_seen)
        ):
            # the STT service reports its own usage: keep the PCM count only as a fallback for the end of call
            if not self._stt_usage_seen:
                self._pcm_pending_seconds += self._audio_bytes / self._audio_bytes_per_second
        elif self._audio_bytes and self._audio_bytes_per_second:
            seconds = self._audio_bytes / self._audio_bytes_per_second
            self.call.emit(
                "stt",
                {"audio_input_seconds": round(seconds, 3)},
                src="estimated",
                how={"audio_input_seconds": "pcm_bytes"},
                timing_ms={
                    "ttfb": self._ttfb.pop("stt", None),
                    "processing": self._processing.pop("stt", None),
                },
            )
        self._audio_bytes = 0
        self.call.next_turn()

    def _end_call(self) -> None:
        """No STT usage metrics arrived during the call: fall back to the PCM seconds counted on input."""
        if self._pcm_pending_seconds and not self._stt_usage_seen:
            self.call.emit(
                "stt",
                {"audio_input_seconds": round(self._pcm_pending_seconds, 3)},
                src="estimated",
                how={"audio_input_seconds": "pcm_bytes"},
            )
        self._pcm_pending_seconds = 0.0


def _pipecat_has_stt_usage() -> bool:
    """True when the installed Pipecat reports STT usage (1.x). Only imports the metrics module, lazily."""
    try:
        from pipecat.metrics.metrics import STTUsageMetricsData  # type: ignore  # noqa: F401

        return True
    except Exception:
        return False


_observer_class: type | None = None


def _build_class() -> type:
    global _observer_class
    if _observer_class is not None:
        return _observer_class
    try:
        from pipecat.observers.base_observer import BaseObserver  # type: ignore
    except Exception:  # Pipecat not installed: plain class, still usable in tests
        BaseObserver = object  # type: ignore[assignment,misc]

    bases = (_Core,) if BaseObserver is object else (_Core, BaseObserver)

    class VoiceTollObserver(*bases):  # type: ignore[misc,valid-type]
        def __init__(self, **kwargs: Any) -> None:
            if BaseObserver is not object:
                BaseObserver.__init__(self)
            _Core.__init__(self, **kwargs)

        async def on_push_frame(self, data: Any) -> None:  # Pipecat >= 0.0.60 passes FramePushed
            frame = getattr(data, "frame", data)
            self.handle_frame(frame)

    _observer_class = VoiceTollObserver
    return VoiceTollObserver


def Observer(**kwargs: Any) -> Any:  # noqa: N802  (reads like a class at the call site)
    """Create a Pipecat observer that reports usage and latency to voiceToll."""
    return _build_class()(**kwargs)
