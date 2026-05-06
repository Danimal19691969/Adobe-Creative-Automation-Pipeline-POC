"""AssetManagerAgent — checks for an existing hero in inputs/assets/{pid}/."""

from __future__ import annotations

import logging
from pathlib import Path

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

logger = logging.getLogger(__name__)

_ASSET_EXTENSIONS = ("hero.png", "hero.jpg", "hero.jpeg", "hero.webp")
_ASSET_ROOT = Path("inputs/assets")


class AssetManagerAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        product_dir = _ASSET_ROOT / self.product_id
        hero_path: str | None = None
        for filename in _ASSET_EXTENSIONS:
            candidate = product_dir / filename
            if candidate.exists():
                hero_path = str(candidate)
                break

        product_key = f"product:{self.product_id}"
        product_state = dict(ctx.session.state.get(product_key, {}))

        if hero_path:
            product_state["hero_path"] = hero_path
            product_state["asset_source"] = "reused"
            text = f"Reusing existing hero for {self.product_id}: {hero_path}"
            logger.info(text)
        else:
            product_state["hero_path"] = None
            product_state["asset_source"] = None
            text = f"No hero found for {self.product_id}; image generator will fire."
            logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


asset_manager_agent = AssetManagerAgent(name="AssetManagerAgent")
