"""Imagen 3 backend — used when IMAGE_PROVIDER=google."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)


def generate_with_imagen(
    prompt: str,
    out_path: str,
    aspect_ratio: str = "1:1",
    model: str = "imagen-3.0-generate-002",
) -> dict:
    """Call Imagen 3 via google-genai. Writes PNG to out_path.

    Returns: {path, latency_ms, model}
    """
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY required for Imagen 3 backend")

    client = genai.Client(api_key=api_key)
    started = time.monotonic()
    response = client.models.generate_images(
        model=model,
        prompt=prompt,
        config=types.GenerateImagesConfig(
            number_of_images=1,
            aspect_ratio=aspect_ratio,
        ),
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    images = getattr(response, "generated_images", None) or []
    if not images:
        raise RuntimeError(f"Imagen returned no images for prompt: {prompt!r}")
    image_bytes = images[0].image.image_bytes

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(image_bytes)
    logger.info("Imagen 3 wrote %s in %dms", out_path, latency_ms)

    return {"path": out_path, "latency_ms": latency_ms, "model": model}
