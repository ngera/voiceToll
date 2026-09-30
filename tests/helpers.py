"""Shared test helpers. Tests use unittest so they run under `pytest` or `python -m unittest`."""

from __future__ import annotations

import tempfile
import types
from pathlib import Path

from voicetoll.client import VoiceToll
from voicetoll.config import Config


def offline_client(**overrides) -> VoiceToll:
    """A client whose exporter never starts, so events stay in the buffer for inspection."""
    config = Config(
        endpoint="http://127.0.0.1:9", project="test", hmac_key="test-secret", env="test", region="us-east"
    ).with_overrides(**overrides)
    client = VoiceToll(config)
    client._started = True  # suppress the background thread
    return client


def drain(client: VoiceToll) -> list[dict]:
    return client.buffer.take(10_000)


def fake(type_name: str, **attrs):
    """An object whose class name is `type_name`, for duck-typed adapter tests."""
    cls = type(type_name, (), {})
    obj = cls()
    for key, value in attrs.items():
        setattr(obj, key, value)
    return obj


def ns(**attrs):
    return types.SimpleNamespace(**attrs)


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="voicetoll-test-"))
