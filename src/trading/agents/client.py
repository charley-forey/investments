"""Anthropic client factory."""

from __future__ import annotations

from ..config import Config


def make_client(config: Config):
    import anthropic

    if not config.secrets.anthropic_api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=config.secrets.anthropic_api_key)
