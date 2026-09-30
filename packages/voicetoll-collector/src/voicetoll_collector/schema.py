"""Capture event schema (v1). Validation doubles as the ingest allow-list scrub.

Unknown top-level fields are ignored; unknown unit, timing and tag keys are dropped; strings are
length-capped. Nothing free-form (text, URLs, headers, error messages) has a field to land in.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COMPONENTS = ("stt", "llm", "tts", "s2s", "vad", "telephony", "platform", "turn")
UNIT_NAMES = (
    "characters",
    "audio_input_seconds",
    "audio_output_seconds",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "input_audio_tokens",
    "output_audio_tokens",
    "cache_audio_read_tokens",
    "agent_minutes",
    "telephony_minutes",
)
TIMING_NAMES = ("ttfb", "ttft", "duration", "eou_delay", "transcription_delay", "processing")
TAG_KEYS = ("feature", "agent_version", "env", "region", "caller_country")
MAX_TAG_LEN = 64
MAX_ID_LEN = 128

Id = Field(default=None, max_length=MAX_ID_LEN)


def _finite_non_negative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number) or number < 0:
        return None
    return number


class CaptureEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    event_id: str = Field(min_length=8, max_length=64)
    schema_version: int = Field(default=1, alias="schema")
    source: str = Field(default="sdk", max_length=32)
    ts: datetime
    project: str = Field(default="default", max_length=64)
    tenant: str | None = Id
    user: str | None = Id
    session: str = Field(min_length=1, max_length=MAX_ID_LEN)
    turn: int | None = Field(default=None, ge=0)
    component: Literal["stt", "llm", "tts", "s2s", "vad", "telephony", "platform", "turn"]
    provider: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=MAX_ID_LEN)
    voice_class: str | None = Field(default=None, max_length=64)
    units: dict[str, float] = Field(default_factory=dict)
    src: dict[str, Literal["reported", "estimated"]] = Field(default_factory=dict)
    how: dict[str, str] = Field(default_factory=dict)
    timing_ms: dict[str, float] = Field(default_factory=dict)
    status: str = Field(default="ok", max_length=32)
    cancelled: bool = False
    request_id: str | None = Id
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("ts", mode="before")
    @classmethod
    def _parse_ts(cls, value: Any) -> Any:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(float(value), tz=UTC)
        return value

    @field_validator("ts")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @field_validator("provider", mode="before")
    @classmethod
    def _lower(cls, value: Any) -> Any:
        # Frameworks sometimes report the API host ("api.openai.com"); store the provider name so pricing,
        # breakdowns and reconciliation accounts all use one spelling.
        if not isinstance(value, str):
            return value
        name = value.strip().lower()
        if "." in name:
            parts = [p for p in name.split(".") if p not in ("api", "www")]
            if len(parts) >= 2:
                name = parts[-2]
        return name

    @field_validator("units", mode="before")
    @classmethod
    def _units(cls, value: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, raw in (value or {}).items():
            number = _finite_non_negative(raw)
            if key in UNIT_NAMES and number:
                out[key] = number
        return out

    @field_validator("timing_ms", mode="before")
    @classmethod
    def _timing(cls, value: Any) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, raw in (value or {}).items():
            number = _finite_non_negative(raw)
            if key in TIMING_NAMES and number is not None:
                out[key] = number
        return out

    @field_validator("src", mode="before")
    @classmethod
    def _src(cls, value: Any) -> dict[str, str]:
        return {k: v for k, v in (value or {}).items() if k in UNIT_NAMES and v in ("reported", "estimated")}

    @field_validator("how", mode="before")
    @classmethod
    def _how(cls, value: Any) -> dict[str, str]:
        return {k: str(v)[:48] for k, v in (value or {}).items() if k in UNIT_NAMES}

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, raw in (value or {}).items():
            if key in TAG_KEYS and raw is not None and str(raw).strip():
                out[key] = str(raw).strip()[:MAX_TAG_LEN]
        return out

    @property
    def day(self) -> str:
        return self.ts.strftime("%Y-%m-%d")

    def unit_source(self, unit: str) -> str:
        return self.src.get(unit, "reported")


class ClientStats(BaseModel):
    """Counters the client sends with each batch (`"client"` next to `"events"`). Allow-listed like events:
    unknown keys are dropped and nothing free-form has a field to land in."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    client_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    sdk_version: str | None = Field(default=None, max_length=32)
    dropped: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    sent: int = Field(default=0, ge=0)
    buffer_len: int = Field(default=0, ge=0)
    buffer_max: int = Field(default=0, ge=0)
    started_epoch: float | None = Field(default=None, ge=0)
