"""Compatibility path: `voicetoll.livekit` is `voicetoll.frameworks.livekit` (same module object)."""

from __future__ import annotations

import sys

from .frameworks import livekit as _impl

sys.modules[__name__] = _impl
