"""Framework adapters: one module per voice framework, found through a small registry.

Each adapter turns what its framework already measures into `CallSession.emit(...)` calls. The collector
never sees framework objects, only the framework-neutral capture event, so it does not care which
framework (if any) produced an event. Apps on a framework without an adapter use `voicetoll.record()`
or send OpenTelemetry spans to the collector's OTLP endpoint.

Built-in adapters:

    livekit   voicetoll.frameworks.livekit:attach     LiveKit Agents 1.x (metrics_collected events)
    pipecat   voicetoll.frameworks.pipecat:Observer   Pipecat (MetricsFrame observer)

Third-party adapters register through the `voicetoll.frameworks` entry point group, e.g. in the adapter
package's pyproject.toml:

    [project.entry-points."voicetoll.frameworks"]
    ten = "voicetoll_ten:attach"

and are then available as `voicetoll.frameworks.load("ten")`. Writing an adapter: see `base.py`.
Importing this package imports no framework and no adapter; `load()` imports on first use.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("voicetoll")

ENTRY_POINT_GROUP = "voicetoll.frameworks"


@dataclass(frozen=True)
class Framework:
    name: str  # also the `source` field on every event the adapter emits
    target: str  # "package.module:callable" that instruments one call
    description: str = ""


_BUILTIN: dict[str, Framework] = {
    "livekit": Framework("livekit", "voicetoll.frameworks.livekit:attach", "LiveKit Agents 1.x"),
    "pipecat": Framework("pipecat", "voicetoll.frameworks.pipecat:Observer", "Pipecat metrics observer"),
}
_registered: dict[str, Framework] = {}


def register(name: str, target: str, description: str = "") -> None:
    """Register an adapter at runtime (entry points are the usual route for packages)."""
    _registered[name] = Framework(name, target, description)


def available() -> dict[str, Framework]:
    """Built-in, entry-point and runtime-registered adapters, by name. Later sources override earlier."""
    found = dict(_BUILTIN)
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            found[ep.name] = Framework(ep.name, ep.value, getattr(ep.dist, "name", "") or "")
    except Exception as exc:  # a broken third-party package must not break the app
        log.warning("voicetoll: could not read %s entry points: %s", ENTRY_POINT_GROUP, exc)
    found.update(_registered)
    return found


def load(name: str) -> Any:
    """Return the instrumenting callable of adapter `name` (imports it on first use)."""
    framework = available().get(name)
    if framework is None:
        raise KeyError(f"no voiceToll framework adapter named {name!r}; available: {sorted(available())}")
    module_name, _, attr = framework.target.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr) if attr else module


__all__ = ["ENTRY_POINT_GROUP", "Framework", "available", "load", "register"]
