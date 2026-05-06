"""BriefParserAgent — parses the campaign brief and validates against the loaded brand."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.schemas import CampaignBrief

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "inputs/campaign_briefs/summer_refresh_2025.yaml"


class BriefParserAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        path = Path(os.environ.get("CAMPAIGN_BRIEF_PATH", _DEFAULT_PATH))
        if not path.exists():
            raise FileNotFoundError(f"Campaign brief not found: {path}")

        with path.open() as f:
            raw = yaml.safe_load(f)

        brief = CampaignBrief.model_validate(raw)

        brand = ctx.session.state.get("brand")
        if brand is None:
            raise RuntimeError("BrandLoaderAgent must run before BriefParserAgent")
        if brand["brand_id"] != brief.brand_id:
            raise ValueError(
                f"Brief brand_id={brief.brand_id!r} does not match loaded "
                f"brand_id={brand['brand_id']!r}"
            )

        logger.info(
            "Parsed campaign brief: campaign_id=%s products=%s markets=%s",
            brief.campaign_id, [p.id for p in brief.products], brief.markets,
        )

        # Per-product state lives at flat keys "product:{pid}" so that parallel
        # product branches can each update their own key without racing siblings.
        # state_delta is shallow-merged by the session service, so nested writes
        # under a single "products" dict would clobber each other.
        product_ids = [p.id for p in brief.products]
        per_product_seeds = {f"product:{pid}": {} for pid in product_ids}

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Loaded campaign '{brief.campaign_id}' with "
                    f"{len(brief.products)} products and {len(brief.markets)} markets."
                ))],
            ),
            actions=EventActions(state_delta={
                "brief": brief.model_dump(mode="json"),
                "product_ids": product_ids,
                **per_product_seeds,
            }),
        )


brief_parser_agent = BriefParserAgent(name="BriefParserAgent")
