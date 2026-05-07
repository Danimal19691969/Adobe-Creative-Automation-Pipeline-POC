"""AssetManagerAgent — locates the source hero image for a product.

Source-of-truth precedence (gated by the brief's force/regenerate flags):
  1. inputs/assets/{pid}/hero.{png,jpg,jpeg,webp}   — user-supplied
       skipped when brief.force_generate_hero is True
  2. outputs/{pid}/source/global_*.png              — previously generated
       skipped when brief.regenerate_cached_assets is True
       sidecar JSON next to the image carries asset_source / image_model /
       image_gen_latency_ms so cache hits surface true provenance in the report
  3. otherwise: leave hero_path=None so ImageGeneratorAgent fires the API
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

logger = logging.getLogger(__name__)

_USER_ASSET_FILENAMES = ("hero.png", "hero.jpg", "hero.jpeg", "hero.webp")
_USER_ASSET_ROOT = Path("inputs/assets")
_GENERATED_SOURCE_ROOT = Path("outputs")  # /{pid}/source/global_{ts}.png


def _find_user_supplied(product_id: str) -> str | None:
    product_dir = _USER_ASSET_ROOT / product_id
    for filename in _USER_ASSET_FILENAMES:
        candidate = product_dir / filename
        if candidate.exists():
            return str(candidate)
    return None


def _find_generated_cache(product_id: str) -> tuple[str | None, dict | None]:
    """Return (path, sidecar_metadata) for the most recent previously-generated
    source hero, or (None, None) if no cache exists."""
    source_dir = _GENERATED_SOURCE_ROOT / product_id / "source"
    if not source_dir.exists():
        return None, None
    candidates = sorted(source_dir.glob("global_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None
    latest = candidates[0]
    sidecar = latest.with_suffix(".json")
    metadata: dict = {}
    if sidecar.exists():
        try:
            metadata = json.loads(sidecar.read_text())
        except json.JSONDecodeError:
            metadata = {}
    return str(latest), metadata


class AssetManagerAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brief = state.get("brief", {}) or {}
        force_generate = bool(brief.get("force_generate_hero", False))
        regen_cache = bool(brief.get("regenerate_cached_assets", False))

        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))

        hero_path: str | None = None
        asset_source: str | None = None
        used_cache = False
        image_model: str | None = None
        image_gen_latency_ms: int | None = None
        image_provider: str | None = None

        # Tier 1: user-supplied hero (skipped when force_generate_hero=True)
        if not force_generate:
            user_path = _find_user_supplied(self.product_id)
            if user_path:
                hero_path = user_path
                asset_source = "user_supplied"
                used_cache = True

        # Tier 2: previously-generated source (skipped when regenerate_cached_assets=True)
        if hero_path is None and not regen_cache:
            cached_path, sidecar = _find_generated_cache(self.product_id)
            if cached_path:
                hero_path = cached_path
                used_cache = True
                if sidecar:
                    asset_source = sidecar.get("asset_source")
                    image_model = sidecar.get("image_model")
                    image_gen_latency_ms = sidecar.get("image_gen_latency_ms")
                    image_provider = sidecar.get("image_provider")
                else:
                    asset_source = "generated_cached"

        product_state["hero_path"] = hero_path
        product_state["asset_source"] = asset_source
        product_state["used_cache"] = used_cache
        product_state["image_model"] = image_model
        product_state["image_gen_latency_ms"] = image_gen_latency_ms
        product_state["image_provider"] = image_provider

        if hero_path:
            text = (
                f"Located hero for {self.product_id} ({asset_source}, "
                f"used_cache={used_cache}, force_generate={force_generate}, "
                f"regen_cache={regen_cache}): {hero_path}"
            )
        else:
            text = (
                f"No hero located for {self.product_id} "
                f"(force_generate={force_generate}, regen_cache={regen_cache}); "
                f"image generator will fire."
            )
        logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


asset_manager_agent = AssetManagerAgent(name="AssetManagerAgent")
