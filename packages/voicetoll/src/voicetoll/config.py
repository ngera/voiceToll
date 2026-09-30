"""Client configuration, read from the environment and overridable in code."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

_REGION_ENV_VARS = ("VOICETOLL_REGION", "FLY_REGION", "AWS_REGION", "GOOGLE_CLOUD_REGION", "REGION")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _detect_region() -> str | None:
    for name in _REGION_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


@dataclass(frozen=True)
class Config:
    endpoint: str = "http://localhost:4319"
    project: str = "default"
    ingest_key: str | None = None
    hmac_key: str | None = None
    env: str | None = None
    region: str | None = None
    disabled: bool = False

    buffer_size: int = 10_000  # events held in memory at most (about 5 MB)
    batch_size: int = 200  # events per POST
    flush_interval: float = 1.0  # seconds between background flushes
    max_retry_seconds: float = 60.0  # how long a failing batch is retried before it is dropped
    request_timeout: float = 5.0

    extra_tags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            endpoint=os.environ.get("VOICETOLL_ENDPOINT", cls.endpoint),
            project=os.environ.get("VOICETOLL_PROJECT", cls.project),
            ingest_key=os.environ.get("VOICETOLL_INGEST_KEY") or None,
            hmac_key=os.environ.get("VOICETOLL_HMAC_KEY") or None,
            env=os.environ.get("VOICETOLL_ENV") or None,
            region=_detect_region(),
            disabled=_env_bool("VOICETOLL_DISABLED"),
        )

    def with_overrides(self, **overrides: object) -> Config:
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)  # type: ignore[arg-type]
