"""Image-generation dispatcher — single tool surface for the LlmAgent.

Backend chosen by IMAGE_PROVIDER env var (defaults to PROVIDER for google/openai;
required explicitly for anthropic). Strict coupling: no silent fallback.
"""

from __future__ import annotations

import os
from typing import Callable

from .imagen_tool import generate_with_imagen
from .openai_image_tool import generate_with_gpt_image_1

# IMAGE_PROVIDER → (image_fn, required-API-key, default-model, model-env-var)
_IMAGE_BACKENDS: dict[str, tuple[Callable, str, str, str]] = {
    "google": (generate_with_imagen,      "GOOGLE_API_KEY", "imagen-3.0-generate-002", "IMAGE_MODEL"),
    "openai": (generate_with_gpt_image_1, "OPENAI_API_KEY", "gpt-image-1",             "IMAGE_MODEL"),
}

# Chat providers with a native image model — IMAGE_PROVIDER defaults to PROVIDER for these.
_PROVIDER_HAS_IMAGE = {"google", "openai"}


def _resolve_image_provider() -> str:
    explicit = os.environ.get("IMAGE_PROVIDER")
    if explicit:
        return explicit.lower()
    chat = os.environ.get("PROVIDER", "openai").lower()
    if chat in _PROVIDER_HAS_IMAGE:
        return chat
    raise ValueError(
        f"PROVIDER={chat!r} has no native image-gen model. "
        "Set IMAGE_PROVIDER=google or IMAGE_PROVIDER=openai explicitly."
    )


def generate_hero_image(prompt: str, out_path: str, aspect_ratio: str = "1:1") -> dict:
    """Generate a hero image for the campaign and save it to out_path.

    Backend selected by the IMAGE_PROVIDER environment variable:
      - "google" → Imagen 3
      - "openai" → gpt-image-1
    Defaults to PROVIDER when PROVIDER is google or openai. When PROVIDER=anthropic,
    IMAGE_PROVIDER must be set explicitly because Anthropic has no native image API.

    Args:
        prompt: Text prompt describing the hero image to generate.
        out_path: Filesystem path where the PNG will be written.
        aspect_ratio: One of "1:1", "9:16", "16:9". Defaults to "1:1".

    Returns:
        A dict with keys: path (str), latency_ms (int), model (str), provider (str),
        asset_source (str — e.g. "openai_generated" or "imagen_generated").
    """
    image_provider = _resolve_image_provider()
    if image_provider not in _IMAGE_BACKENDS:
        raise ValueError(
            f"Unsupported IMAGE_PROVIDER={image_provider!r}. "
            f"Use one of: {sorted(_IMAGE_BACKENDS)}"
        )
    fn, key_env, default_model, model_env = _IMAGE_BACKENDS[image_provider]
    if not os.environ.get(key_env):
        raise ValueError(f"IMAGE_PROVIDER={image_provider} requires {key_env}")
    model = os.environ.get(model_env, default_model)
    result = fn(prompt=prompt, out_path=out_path, aspect_ratio=aspect_ratio, model=model)
    # Augment with provenance fields the report needs.
    result.setdefault("provider", image_provider)
    result.setdefault("asset_source", f"{image_provider}_generated")
    return result
