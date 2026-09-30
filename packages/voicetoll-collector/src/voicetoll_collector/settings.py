"""Collector settings from VOICETOLL_* environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def parse_ingest_keys(raw: str | None) -> dict[str, str]:
    """'demo=dev-key,acme=k2' -> {'dev-key': 'demo', 'k2': 'acme'} (key -> project)."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        project, key = part.split("=", 1)
        if project.strip() and key.strip():
            out[key.strip()] = project.strip()
    return out


@dataclass
class Settings:
    db_url: str = "sqlite:///./voicetoll.db"
    ingest_keys: dict[str, str] = field(default_factory=dict)
    spool_dir: str = "./spool"
    spool_max_bytes: int = 1024 * 1024 * 1024
    rate_cards_path: str | None = None
    max_body_bytes: int = 5 * 1024 * 1024
    replay_interval_seconds: float = 10.0
    jobs_interval_seconds: float = 3600.0
    recon_drift_threshold: float = 0.05
    tenant_prices_path: str | None = None
    # Highlights
    v2v_budget_ms: float = 1200.0  # voice-to-voice budget per turn (caller stops -> first agent audio)
    release_change_threshold: float = 0.15  # "change after a release" fires beyond +/-15%
    highlight_min_calls: int = 3  # calls per agent version before comparing versions
    highlight_min_turns: int = 20  # turns before latency highlights are trusted
    # Pricing upkeep
    pricing_check_interval_seconds: float = 60.0  # how often rate cards and price versions are checked
    auto_reprice_days: int = 45  # history re-costed when prices change; 0 turns automatic repricing off
    # Admin UI (/admin and /v1/admin/*): off unless a key is set, except in open (dev) mode
    admin_key: str = ""
    rate_card_review_days: int = 90  # a rate card entry whose `reviewed` date is older than this is stale

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            db_url=os.environ.get("VOICETOLL_DB_URL", cls.db_url),
            ingest_keys=parse_ingest_keys(os.environ.get("VOICETOLL_INGEST_KEYS")),
            spool_dir=os.environ.get("VOICETOLL_SPOOL_DIR", cls.spool_dir),
            spool_max_bytes=int(float(os.environ.get("VOICETOLL_SPOOL_MAX_MB", "1024")) * 1024 * 1024),
            rate_cards_path=os.environ.get("VOICETOLL_RATE_CARDS") or None,
            max_body_bytes=int(float(os.environ.get("VOICETOLL_MAX_BODY_MB", "5")) * 1024 * 1024),
            jobs_interval_seconds=float(os.environ.get("VOICETOLL_JOBS_INTERVAL_SECONDS", "3600")),
            recon_drift_threshold=float(os.environ.get("VOICETOLL_RECON_DRIFT_THRESHOLD", "0.05")),
            tenant_prices_path=os.environ.get("VOICETOLL_TENANT_PRICES") or None,
            v2v_budget_ms=float(os.environ.get("VOICETOLL_V2V_BUDGET_MS", "1200")),
            release_change_threshold=float(os.environ.get("VOICETOLL_RELEASE_CHANGE_THRESHOLD", "0.15")),
            highlight_min_calls=int(os.environ.get("VOICETOLL_HIGHLIGHT_MIN_CALLS", "3")),
            highlight_min_turns=int(os.environ.get("VOICETOLL_HIGHLIGHT_MIN_TURNS", "20")),
            pricing_check_interval_seconds=float(os.environ.get("VOICETOLL_PRICING_CHECK_SECONDS", "60")),
            auto_reprice_days=int(os.environ.get("VOICETOLL_AUTO_REPRICE_DAYS", "45")),
            admin_key=os.environ.get("VOICETOLL_ADMIN_KEY", ""),
            rate_card_review_days=int(os.environ.get("VOICETOLL_RATE_CARD_REVIEW_DAYS", "90")),
        )
