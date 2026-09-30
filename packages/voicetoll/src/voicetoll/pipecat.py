"""Compatibility path: `voicetoll.pipecat` is `voicetoll.frameworks.pipecat` (same module object)."""

from __future__ import annotations

import sys

from .frameworks import pipecat as _impl

sys.modules[__name__] = _impl
