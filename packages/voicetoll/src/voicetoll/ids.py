"""Event ids and pseudonymization of tenant and user ids."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

_PREFIX = "h:"


def new_event_id() -> str:
    """Time-ordered unique id: 13 hex chars of nanoseconds + 12 random hex chars."""
    return f"{time.time_ns():x}{os.urandom(6).hex()}"


def pseudonymize(value: str | None, key: str | None) -> str | None:
    """HMAC-SHA256 an id with the project's secret so raw ids never leave the app.

    Values already pseudonymized (prefixed "h:") pass through unchanged. Without a key the value is
    returned as-is; callers should then pass opaque ids only.
    """
    if value is None or value == "":
        return None
    value = str(value)
    if value.startswith(_PREFIX) or not key:
        return value
    digest = hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
    return _PREFIX + digest[:24]
