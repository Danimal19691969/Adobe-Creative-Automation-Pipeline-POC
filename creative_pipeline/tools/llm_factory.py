"""LiteLLM factory — `make_llm()` reads PROVIDER + MODEL env vars and returns
a `LiteLlm` instance ready to pass as `LlmAgent(model=...)`."""

from __future__ import annotations

import os

from google.adk.models.lite_llm import LiteLlm

# PROVIDER (env) → (LiteLLM model-string prefix, required API-key env var)
_PROVIDERS: dict[str, tuple[str, str]] = {
    "google":    ("gemini",    "GOOGLE_API_KEY"),
    "openai":    ("openai",    "OPENAI_API_KEY"),
    "anthropic": ("anthropic", "ANTHROPIC_API_KEY"),
}


def make_llm() -> LiteLlm:
    """Build a LiteLLM-backed model from PROVIDER and MODEL env vars."""
    provider = os.environ.get("PROVIDER", "openai").lower()
    model = os.environ.get("MODEL", "gpt-4o-mini")
    if provider not in _PROVIDERS:
        raise ValueError(
            f"Unsupported PROVIDER={provider!r}. "
            f"Use one of: {sorted(_PROVIDERS)}"
        )
    prefix, key_env = _PROVIDERS[provider]
    if not os.environ.get(key_env):
        raise ValueError(f"PROVIDER={provider} requires {key_env} to be set")
    return LiteLlm(model=f"{prefix}/{model}")
