"""OTLP/HTTP Trace ingest → CaptureEvent list.

Accepts ExportTraceServiceRequest as JSON (application/json) or protobuf
(application/x-protobuf) when opentelemetry-proto is installed.

Maps common GenAI / voice span attributes onto the voiceToll allow-list. Unknown
attributes are ignored.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .ids_otlp import otlp_event_id

# Attribute keys we recognize (OTel GenAI + common voice extensions)
_ATTR_PROVIDER = ("gen_ai.system", "gen_ai.provider.name", "voicetoll.provider", "server.address")
_ATTR_MODEL = ("gen_ai.request.model", "gen_ai.response.model", "voicetoll.model")
_ATTR_COMPONENT = ("voicetoll.component", "gen_ai.operation.name")
_ATTR_SESSION = ("voicetoll.session", "session.id", "gen_ai.conversation.id")
_ATTR_TENANT = ("voicetoll.tenant", "tenant.id")
_ATTR_USER = ("voicetoll.user", "user.id", "enduser.id")
_ATTR_TURN = ("voicetoll.turn", "gen_ai.conversation.turn")
_ATTR_FEATURE = ("voicetoll.feature",)
_ATTR_AGENT_VERSION = ("voicetoll.agent_version",)
_ATTR_REGION = ("voicetoll.region", "cloud.region")
_ATTR_CANCELLED = ("voicetoll.cancelled",)

_UNIT_ATTRS = {
    "gen_ai.usage.input_tokens": "input_tokens",
    "gen_ai.usage.output_tokens": "output_tokens",
    "gen_ai.usage.cache_read_input_tokens": "cache_read_tokens",
    "voicetoll.units.characters": "characters",
    "voicetoll.units.audio_input_seconds": "audio_input_seconds",
    "voicetoll.units.audio_output_seconds": "audio_output_seconds",
    "voicetoll.units.input_audio_tokens": "input_audio_tokens",
    "voicetoll.units.output_audio_tokens": "output_audio_tokens",
    "voicetoll.units.agent_minutes": "agent_minutes",
    "voicetoll.units.telephony_minutes": "telephony_minutes",
}

_TIMING_ATTRS = {
    "voicetoll.timing.ttfb_ms": "ttfb",
    "voicetoll.timing.ttft_ms": "ttft",
    "voicetoll.timing.duration_ms": "duration",
    "voicetoll.timing.eou_delay_ms": "eou_delay",
    "voicetoll.timing.transcription_delay_ms": "transcription_delay",
    "voicetoll.timing.processing_ms": "processing",
    "gen_ai.server.time_to_first_token": "ttft",  # often seconds — normalized below
}

_COMPONENT_FROM_OP = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "stt": "stt",
    "tts": "tts",
    "speech_to_text": "stt",
    "text_to_speech": "tts",
    "realtime": "s2s",
}


def _attrs_to_dict(raw: Any) -> dict[str, Any]:
    """Normalize OTLP attribute list or map into a plain dict of Python values."""
    if isinstance(raw, dict):
        return {str(k): _any_value(v) for k, v in raw.items()}
    out: dict[str, Any] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key:
            continue
        out[str(key)] = _any_value(item.get("value", item.get("v")))
    return out


def _any_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        for k in ("stringValue", "string_value"):
            if k in value:
                return value[k]
        for k in ("intValue", "int_value"):
            if k in value:
                try:
                    return int(value[k])
                except (TypeError, ValueError):
                    return value[k]
        for k in ("doubleValue", "double_value"):
            if k in value:
                return float(value[k])
        for k in ("boolValue", "bool_value"):
            if k in value:
                return bool(value[k])
    return value


def _first(attrs: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in attrs and attrs[key] not in (None, ""):
            return attrs[key]
    return None


def _component(attrs: dict[str, Any], span_name: str) -> str | None:
    raw = _first(attrs, _ATTR_COMPONENT)
    if raw is None:
        raw = span_name
    text = str(raw).lower().replace("-", "_").replace(" ", "_")
    if text in {"stt", "llm", "tts", "s2s", "vad", "telephony", "platform", "turn"}:
        return text
    if text in _COMPONENT_FROM_OP:
        return _COMPONENT_FROM_OP[text]
    for needle, comp in (
        ("tts", "tts"),
        ("stt", "stt"),
        ("llm", "llm"),
        ("chat", "llm"),
        ("realtime", "s2s"),
    ):
        if needle in text:
            return comp
    return None


def _seconds_to_ms_if_needed(key: str, value: float) -> float:
    # gen_ai.server.time_to_first_token is typically seconds when < 100
    if key == "gen_ai.server.time_to_first_token" and value < 100:
        return value * 1000.0
    return value


def span_to_event(span: dict[str, Any], *, default_project: str = "default") -> dict[str, Any] | None:
    attrs = _attrs_to_dict(span.get("attributes") or span.get("attr") or {})
    component = _component(attrs, str(span.get("name") or ""))
    if component is None:
        return None
    session = _first(attrs, _ATTR_SESSION) or span.get("traceId") or span.get("trace_id")
    if not session:
        return None
    units: dict[str, float] = {}
    src: dict[str, str] = {}
    for attr_key, unit in _UNIT_ATTRS.items():
        if attr_key in attrs:
            try:
                units[unit] = float(attrs[attr_key])
                src[unit] = "reported"
            except (TypeError, ValueError):
                pass
    timing_ms: dict[str, float] = {}
    for attr_key, timing in _TIMING_ATTRS.items():
        if attr_key in attrs:
            try:
                timing_ms[timing] = _seconds_to_ms_if_needed(attr_key, float(attrs[attr_key]))
            except (TypeError, ValueError):
                pass
    start_ns = span.get("startTimeUnixNano") or span.get("start_time_unix_nano")
    try:
        ts = int(start_ns) / 1e9 if start_ns is not None else time.time()
    except (TypeError, ValueError):
        ts = time.time()
    end_ns = span.get("endTimeUnixNano") or span.get("end_time_unix_nano")
    if end_ns is not None and "duration" not in timing_ms:
        try:
            timing_ms["duration"] = (int(end_ns) - int(start_ns or end_ns)) / 1e6
        except (TypeError, ValueError):
            pass
    provider = _first(attrs, _ATTR_PROVIDER)
    if isinstance(provider, str):
        provider = provider.lower().split(".")[0]  # strip hostnames somewhat
    model = _first(attrs, _ATTR_MODEL)
    tags: dict[str, str] = {}
    for tag_key, attr_keys in (
        ("feature", _ATTR_FEATURE),
        ("agent_version", _ATTR_AGENT_VERSION),
        ("region", _ATTR_REGION),
    ):
        val = _first(attrs, attr_keys)
        if val is not None:
            tags[tag_key] = str(val)[:64]
    turn_raw = _first(attrs, _ATTR_TURN)
    turn = None
    if turn_raw is not None:
        try:
            turn = int(turn_raw)
        except (TypeError, ValueError):
            pass
    cancelled = bool(_first(attrs, _ATTR_CANCELLED) or False)
    span_id = span.get("spanId") or span.get("span_id") or ""
    event_id = otlp_event_id(str(session), str(span_id), component)
    return {
        "event_id": event_id,
        "schema": 1,
        "source": "otlp",
        "ts": ts,
        "project": default_project,
        "tenant": (str(_first(attrs, _ATTR_TENANT)) if _first(attrs, _ATTR_TENANT) is not None else None),
        "user": (str(_first(attrs, _ATTR_USER)) if _first(attrs, _ATTR_USER) is not None else None),
        "session": str(session)[:128],
        "turn": turn,
        "component": component,
        "provider": provider,
        "model": str(model) if model is not None else None,
        "units": units,
        "src": src,
        "how": {},
        "timing_ms": timing_ms,
        "status": "ok",
        "cancelled": cancelled,
        "tags": tags,
    }


def iter_spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    for rs in payload.get("resourceSpans") or payload.get("resource_spans") or []:
        for ss in (
            rs.get("scopeSpans") or rs.get("scope_spans") or rs.get("instrumentationLibrarySpans") or []
        ):
            for span in ss.get("spans") or []:
                if isinstance(span, dict):
                    spans.append(span)
    return spans


def otlp_json_to_events(payload: dict[str, Any], *, project: str) -> list[dict[str, Any]]:
    events = []
    for span in iter_spans(payload):
        event = span_to_event(span, default_project=project)
        if event is not None:
            events.append(event)
    return events


def parse_otlp_body(body: bytes, content_type: str | None) -> dict[str, Any]:
    ctype = (content_type or "application/json").split(";")[0].strip().lower()
    if ctype in ("application/json", "application/x-protobuf+json", "text/json"):
        return json.loads(body)
    if ctype in ("application/x-protobuf", "application/protobuf", "application/grpc+proto"):
        try:
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        except ImportError as exc:
            raise ValueError("protobuf OTLP requires opentelemetry-proto") from exc
        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        # Convert via protobuf JSON for a uniform path
        from google.protobuf.json_format import MessageToDict

        return MessageToDict(req, preserving_proto_field_name=False)
    # Default: try JSON
    return json.loads(body)
