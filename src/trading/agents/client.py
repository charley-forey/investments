"""LLM client factories."""

from __future__ import annotations

from ..config import Config


def make_client(config: Config):
    import anthropic

    if not config.secrets.anthropic_api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY in .env")
    return anthropic.Anthropic(api_key=config.secrets.anthropic_api_key)


_openai_singleton = None


def openai_client(config: Config | None = None):
    """OpenAI client, for models routed there by `model_by_cycle`.

    Cached: the agent loop constructs a provider per call, and a fresh HTTP client
    per session would throw away connection reuse for no reason.
    """
    global _openai_singleton
    if _openai_singleton is None:
        import openai

        from ..config import get_config

        cfg = config or get_config()
        if not cfg.secrets.openai_api_key:
            raise RuntimeError(
                "Missing OPENAI_API_KEY in .env -- required because a cycle is "
                "configured to use a gpt-* model")
        _openai_singleton = openai.OpenAI(api_key=cfg.secrets.openai_api_key)
    return _openai_singleton
