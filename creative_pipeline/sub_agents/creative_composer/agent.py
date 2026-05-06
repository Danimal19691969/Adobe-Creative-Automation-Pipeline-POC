"""CreativeComposerAgent — produces three aspect ratios × N markets per product, no LLM."""

from __future__ import annotations

import logging
import os

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.tools.file_utils import ASPECT_RATIOS, output_path, pick_locale
from creative_pipeline.tools.pillow_composer import compose_creative

logger = logging.getLogger(__name__)


class CreativeComposerAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brand = BrandGuidelines.model_validate(state["brand"])
        brief = CampaignBrief.model_validate(state["brief"])
        product = next((p for p in brief.products if p.id == self.product_id), None)
        if product is None:
            raise RuntimeError(f"Product {self.product_id!r} not found in brief")

        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))
        hero_path = product_state.get("hero_path")
        if not hero_path or not os.path.exists(hero_path):
            raise RuntimeError(
                f"Hero image not available for product {self.product_id!r}; "
                f"AssetManagerAgent or ImageGeneratorAgent must run first."
            )

        output_dir = os.environ.get("OUTPUT_DIR", "outputs")
        available_locales = list(brief.campaign_message.keys())

        outputs: list[dict] = []
        for market in brief.markets:
            locale = pick_locale(market, available_locales)
            headline = brief.campaign_message.get(locale) or brief.campaign_message["en"]
            disclaimer = brand.legal.required_disclaimers.get(market)

            for ratio in ASPECT_RATIOS:
                out_path = output_path(output_dir, self.product_id, ratio, market, locale)
                meta = compose_creative(
                    hero_path=hero_path,
                    ratio=ratio,
                    headline=headline,
                    disclaimer=disclaimer,
                    guidelines=brand,
                    out_path=out_path,
                )
                meta.update({"market": market, "locale": locale})
                outputs.append(meta)
                logger.info(
                    "composed %s/%s %s_%s in %dms", self.product_id, ratio, market, locale, meta["duration_ms"]
                )

        product_state["outputs"] = outputs

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Composed {len(outputs)} creatives for {self.product_id} "
                    f"({len(brief.markets)} markets × {len(ASPECT_RATIOS)} ratios)."
                ))],
            ),
            actions=EventActions(state_delta={product_key: product_state}),
        )


# Default singleton — used when product_id is set at root-assembly time (Phase 5).
creative_composer_agent = CreativeComposerAgent(name="CreativeComposerAgent")
