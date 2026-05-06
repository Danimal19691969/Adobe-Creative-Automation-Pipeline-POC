"""OpenAI gpt-image-1 backend — used when IMAGE_PROVIDER=openai."""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path

from openai import OpenAI

logger = logging.getLogger(__name__)


_ASPECT_TO_SIZE: dict[str, str] = {
    "1:1":  "1024x1024",
    "9:16": "1024x1536",
    "16:9": "1536x1024",
}


def _aspect_to_size(aspect_ratio: str) -> str:
    if aspect_ratio not in _ASPECT_TO_SIZE:
        # Default to square if unknown — composer smart-crops anyway.
        logger.warning("Unknown aspect_ratio %r, defaulting to 1024x1024", aspect_ratio)
        return "1024x1024"
    return _ASPECT_TO_SIZE[aspect_ratio]


def generate_with_gpt_image_1(
    prompt: str,
    out_path: str,
    aspect_ratio: str = "1:1",
    model: str = "gpt-image-1",
) -> dict:
    """Call OpenAI's gpt-image-1. Writes PNG to out_path.

    Returns: {path, latency_ms, model}
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY required for gpt-image-1 backend")

    client = OpenAI()
    started = time.monotonic()
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=_aspect_to_size(aspect_ratio),
        n=1,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    if not response.data or not response.data[0].b64_json:
        raise RuntimeError(f"gpt-image-1 returned no image for prompt: {prompt!r}")
    image_bytes = base64.b64decode(response.data[0].b64_json)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(image_bytes)
    logger.info("gpt-image-1 wrote %s in %dms", out_path, latency_ms)

    return {"path": out_path, "latency_ms": latency_ms, "model": model}
