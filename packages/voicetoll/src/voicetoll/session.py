"""Per-call context shared by all adapters: who the call belongs to and which providers it uses."""

from __future__ import annotations

import logging
from typing import Any

from .client import VoiceToll, get_client
from .ids import pseudonymize

log = logging.getLogger("voicetoll")
_warned_no_key = False


class CallSession:
    """Holds tenant, user, tags and the provider/model for each component of one call."""

    def __init__(
        self,
        *,
        session_id: str,
        tenant: str | None = None,
        user: str | None = None,
        feature: str | None = None,
        agent_version: str | None = None,
        caller_country: str | None = None,
        components: dict[str, tuple[str | None, str | None]] | None = None,
        source: str = "sdk",
        client: VoiceToll | None = None,
    ) -> None:
        global _warned_no_key
        self.client = client or get_client()
        key = self.client.config.hmac_key
        if not key and (tenant or user) and not _warned_no_key:
            log.warning("voicetoll: VOICETOLL_HMAC_KEY is not set; tenant/user ids are sent as given")
            _warned_no_key = True
        self.session_id = str(session_id)
        self.tenant = pseudonymize(tenant, key)
        self.user = pseudonymize(user, key)
        self.source = source
        self.turn = 0
        self.components: dict[str, tuple[str | None, str | None]] = dict(components or {})
        self.tags: dict[str, str | None] = {
            "feature": feature,
            "agent_version": agent_version,
            "caller_country": caller_country,
        }

    # ---- context changes -------------------------------------------------------------------
    def set_component(self, component: str, provider: str | None, model: str | None) -> None:
        self.components[component] = (provider, model)

    def set_feature(self, feature: str | None) -> None:
        """Attribute events from now on to another feature (e.g. the call moved from booking to billing)."""
        self.tags["feature"] = feature

    def next_turn(self) -> int:
        self.turn += 1
        return self.turn

    # ---- emitting --------------------------------------------------------------------------
    def emit(
        self,
        component: str,
        units: dict[str, Any] | None = None,
        *,
        provider: str | None = None,
        model: str | None = None,
        turn: int | None = None,
        **kwargs: Any,
    ) -> bool:
        try:
            reg_provider, reg_model = self.components.get(component, (None, None))
            event = self.client.build_event(
                component,
                provider or reg_provider,
                model or reg_model,
                units,
                session_id=self.session_id,
                tenant=self.tenant,
                user=self.user,
                turn=self.turn if turn is None else turn,
                source=self.source,
                tags=self.tags,
                **kwargs,
            )
            return self.client.enqueue(event)
        except Exception:
            self.client.errors += 1
            return False
