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

        # Cross-validate that the brief's layout_template exists in the brand.
        brand_templates = list(brand.get("layout_templates", {}).keys())
        if brief.layout_template not in brand_templates:
            raise ValueError(
                f"Brief layout_template={brief.layout_template!r} not defined in brand. "
                f"Available templates: {brand_templates}"
            )

        # When localized_copy is True, every market must resolve to a locale that
        # exists in either campaign_message_localized or has en as a fallback.
        if brief.localized_copy:
            market_locales = brand.get("market_locales", {})
            available = set(brief.campaign_message_localized.keys()) | {brief.language}
            for market in brief.markets:
                chain = market_locales.get(market, [brief.language])
                if not any(loc in available for loc in chain):
                    raise ValueError(
                        f"Market {market!r} cannot be served by available locales "
                        f"{sorted(available)} via chain {chain}"
                    )

        logger.info(
            "Parsed campaign brief: campaign_id=%s language=%s localized_copy=%s "
            "layout_template=%s creative_quality=%s products=%s markets=%s",
            brief.campaign_id, brief.language, brief.localized_copy,
            brief.layout_template, brief.creative_quality.value,
            [p.id for p in brief.products], brief.markets,
        )

        # Per-product state lives at flat keys "product:{pid}" so that parallel
        # product branches can each update their own key without racing siblings.
        product_ids = [p.id for p in brief.products]
        per_product_seeds = {f"product:{pid}": {} for pid in product_ids}

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Loaded campaign '{brief.campaign_id}' "
                    f"(language={brief.language}, localized_copy={brief.localized_copy}, "
                    f"layout={brief.layout_template}) — {len(brief.products)} products, "
                    f"{len(brief.markets)} markets."
                ))],
            ),
            actions=EventActions(state_delta={
                "brief": brief.model_dump(mode="json"),
                "product_ids": product_ids,
                **per_product_seeds,
            }),
        )


brief_parser_agent = BriefParserAgent(name="BriefParserAgent")
