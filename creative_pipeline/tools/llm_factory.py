"""LiteLLM factory — `make_llm()` reads PROVIDER + MODEL env vars and returns
a `LiteLlm` instance ready to pass as `LlmAgent(model=...)`."""

from __future__ import annotations

import logging
import os

from google.adk.models.lite_llm import LiteLlm

logger = logging.getLogger(__name__)

# PROVIDER (env) → (LiteLLM model-string prefix, required API-key env var)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "google":    ("gemini",    "GOOGLE_API_KEY"),
    "openai":    ("openai",    "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
}

# One-shot startup log so silent fallback to in-code defaults is visible
# in the run output (root cause of the "always uses openai/gpt-4o-mini"
# bug was that load_dotenv silently no-op'd and the factory fell through
# to defaults without anyone noticing).
_LOGGED_RESOLUTION = False


def make_llm() -> LiteLlm:
    """Build a LiteLLM-backed model from PROVIDER and MODEL env vars."""
    global _LOGGED_RESOLUTION
    provider_raw = os.environ.get("PROVIDER")
    model_raw = os.environ.get("MODEL")
    provider = (provider_raw or "openai").lower()
    model = model_raw or "gpt-4o-mini"
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unsupported PROVIDER={provider!r}. "
            f"Use one of: {sorted(_PROVIDERS)}"
        )
    prefix, key_env = _PROVIDERS[provider]
    if not os.environ.get(key_env):
        raise ValueError(f"PROVIDER={provider} requires {key_env} to be set")
    if not _LOGGED_RESOLUTION:
        source_provider = "env" if provider_raw else "default(openai)"
        source_model = "env" if model_raw else "default(gpt-4o-mini)"
        logger.info(
            "LLM resolved: provider=%s (%s) model=%s (%s)",
            provider, source_provider, model, source_model,
        )
        _LOGGED_RESOLUTION = True
    return LiteLlm(model=f"{prefix}/{model}")
