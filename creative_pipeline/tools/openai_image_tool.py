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

# Map creative_quality (set by the agent into OPENAI_IMAGE_QUALITY) to gpt-image-1's
# quality parameter. "auto" lets the model pick.
_QUALITY_MAP: dict[str, str] = {
    "rough_draft":   "low",
    "demo_polished": "high",
    "production":    "high",
}


def _aspect_to_size(aspect_ratio: str) -> str:
    if aspect_ratio not in _ASPECT_TO_SIZE:
        logger.warning("Unknown aspect_ratio %r, defaulting to 1024x1024", aspect_ratio)
        return "1024x1024"
    return _ASPECT_TO_SIZE[aspect_ratio]


def _resolve_quality() -> str:
    # The agent sets OPENAI_IMAGE_QUALITY from creative_quality; otherwise fall back to "auto".
    raw = os.environ.get("OPENAI_IMAGE_QUALITY", "").lower()
    if raw in {"low", "medium", "high", "auto"}:
        return raw
    return _QUALITY_MAP.get(raw, "auto")


def generate_with_gpt_image_1(
    prompt: str,
    out_path: str,
    aspect_ratio: str = "1:1",
    model: str = "gpt-image-1",
) -> dict:
    """Call OpenAI's gpt-image-1. Writes PNG to out_path.

    Returns: {path, latency_ms, model, provider, asset_source}
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY required for gpt-image-1 backend")

    client = OpenAI()
    quality = _resolve_quality()
    started = time.monotonic()
    response = client.images.generate(
        model=model,
        prompt=prompt,
        size=_aspect_to_size(aspect_ratio),
        n=1,
        quality=quality,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    if not response.data:
        raise RuntimeError(f"gpt-image-1 returned no image for prompt: {prompt!r}")
    b64 = response.data[0].b64_json
    if not b64:
        raise RuntimeError("gpt-image-1 returned empty b64 payload")
    image_bytes = base64.b64decode(b64)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(image_bytes)
    logger.info("gpt-image-1 (quality=%s) wrote %s in %dms", quality, out_path, latency_ms)

    return {
        "path": out_path,
        "latency_ms": latency_ms,
        "model": model,
        "provider": "openai",
        "asset_source": "openai_generated",
        "quality": quality,
    }
