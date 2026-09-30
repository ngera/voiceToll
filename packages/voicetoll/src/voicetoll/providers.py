"""Provider names as voiceToll prices them, shared by every framework adapter and `record()`.

Framework adapters own their framework's naming convention (LiveKit plugin module paths, Pipecat service
class names) and reduce it to a brand token such as "elevenlabs" or "api.openai.com". This module turns
that token into the canonical provider id used by voice-prices, breakdowns and reconciliation accounts,
so that the same provider is spelled the same way whichever framework reported it.

Only aliases checked against voice-prices provider ids belong here. Unknown names pass through lowercased;
the collector then prices them as `unpriced` / `provider_unknown`, never as $0.
"""

from __future__ import annotations

from typing import Any

# API hosts some frameworks report instead of a provider name.
HOSTS: dict[str, str] = {
    "api.openai.com": "openai",
    "api.deepgram.com": "deepgram",
    "api.elevenlabs.io": "elevenlabs",
    "api.cartesia.ai": "cartesia",
    "api.anthropic.com": "anthropic",
    "api.groq.com": "groq",
}

# Brand tokens that differ from the voice-prices provider id (checked against voice-prices 2026-09-27).
ALIASES: dict[str, str] = {
    "azureopenai": "azure",  # Azure OpenAI is billed by Azure, not OpenAI; voice-prices would match "openai"
    "amazon": "aws",
    "bedrock": "aws",
    "awsbedrock": "aws",
    "awstranscribe": "aws",
    "awspolly": "aws",
    "polly": "aws",
    "gemini": "google",
    "googlevertex": "google",
    "vertex": "google",
}


def normalize_provider(provider: Any) -> str | None:
    """'api.openai.com' -> 'openai', 'AzureOpenAI' -> 'azure', 'Deepgram' -> 'deepgram'. Never raises."""
    if not isinstance(provider, str) or not provider.strip():
        return None
    name = provider.strip().lower()
    if name in HOSTS:
        return HOSTS[name]
    if "." in name:  # other hosts: api.<name>.<tld> -> <name>
        parts = [p for p in name.split(".") if p not in ("api", "www")]
        if len(parts) >= 2:
            name = parts[-2]
    compact = name.replace("-", "").replace("_", "").replace(" ", "")
    return ALIASES.get(compact, name)
