"""CreativeComposerAgent — produces every aspect ratio × every market per product.

Localization is **explicit** and driven by the brief:
  - localized_copy=False (default): every (market, ratio) renders the same
    primary ``brief.campaign_message`` in ``brief.language``. No translation,
    no inference. Output filenames use ``{market}_{language}``.
  - localized_copy=True: per-market locale picked via ``brand.market_locales``;
    headline pulled from ``brief.campaign_message_localized[locale]`` (with
    per-product override taking precedence when set).
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.tools.file_utils import output_path, pick_locale
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

        layout = brand.layout_templates.get(brief.layout_template)
        if layout is None:
            raise RuntimeError(
                f"Layout template {brief.layout_template!r} not found in brand "
                f"(BriefParserAgent should have caught this)"
            )

        output_dir = os.environ.get("OUTPUT_DIR", "outputs")
        ratios = list(brand.aspect_ratios.keys())

        outputs: list[dict] = []
        for market in brief.markets:
            # Resolve copy + locale per the explicit localization contract.
            if brief.localized_copy:
                available_locales = list(brief.campaign_message_localized.keys())
                locale = pick_locale(
                    market=market,
                    available_locales=available_locales,
                    market_locales=brand.market_locales,
                    fallback_language=brief.language,
                )
                # Per-product override beats the brief-wide localized message.
                if product.campaign_message:
                    headline = product.campaign_message
                else:
                    headline = (
                        brief.campaign_message_localized.get(locale)
                        or brief.campaign_message
                    )
            else:
                # Localization disabled — single-language run. ``brief.language``
                # is authoritative: when it matches a key in
                # ``campaign_message_localized``, that entry wins over the
                # primary ``campaign_message``. This means a brief author
                # can flip ``language: es`` and the rendered text follows
                # without rewriting ``campaign_message`` — matching what
                # the inline brief comment promises.
                # Resolution order (most specific wins):
                #   1. product.campaign_message                            (per-product override)
                #   2. brief.campaign_message_localized[brief.language]    (NEW — language is a directive)
                #   3. brief.campaign_message                              (fallback)
                locale = brief.language
                headline = (
                    product.campaign_message
                    or brief.campaign_message_localized.get(brief.language)
                    or brief.campaign_message
                )

            # Disclaimer resolution order (most specific wins):
            #   1. brief.disclaimer_text_localized[market]            (when localized_legal_copy=True)
            #   2. brief.disclaimer_text_localized[brief.language]    (fallback localized)
            #   3. brand.legal.required_disclaimers[market]           (when localized_legal_copy=True)
            #   4. brief.disclaimer_text                              (single-language brief override)
            #   5. brand.legal.default_disclaimer                     (compliance boilerplate)
            #
            # The brief is intended to own campaign-specific copy
            # ("Sale ends Dec 31"); brand legal stays as the fallback
            # for compliance boilerplate the marketing team isn't
            # rewriting per campaign.
            disclaimer = None
            if brief.localized_legal_copy:
                disclaimer = (
                    brief.disclaimer_text_localized.get(market)
                    or brief.disclaimer_text_localized.get(brief.language)
                    or brand.legal.required_disclaimers.get(market)
                )
            if disclaimer is None:
                disclaimer = brief.disclaimer_text or brand.legal.default_disclaimer

            for ratio in ratios:
                out_path = output_path(output_dir, self.product_id, ratio, market, locale)
                meta = compose_creative(
                    hero_path=hero_path,
                    ratio=ratio,
                    headline=headline,
                    disclaimer=disclaimer,
                    guidelines=brand,
                    layout=layout,
                    out_path=out_path,
                )
                meta.update({
                    "market": market,
                    "locale": locale,
                    "headline": headline,
                    "disclaimer_text": disclaimer,
                })
                outputs.append(meta)
                logger.info(
                    "composed %s/%s %s_%s in %dms (localized_copy=%s, localized_legal=%s)",
                    self.product_id, ratio, market, locale, meta["duration_ms"],
                    brief.localized_copy, brief.localized_legal_copy,
                )

        product_state["outputs"] = outputs
        product_state["layout_template"] = brief.layout_template
        product_state["language"] = brief.language
        product_state["localized_copy"] = brief.localized_copy
        product_state["localized_legal_copy"] = brief.localized_legal_copy
        product_state["creative_quality"] = brief.creative_quality.value

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Composed {len(outputs)} creatives for {self.product_id} "
                    f"({len(brief.markets)} markets × {len(ratios)} ratios) — "
                    f"language={brief.language}, localized_copy={brief.localized_copy}, "
                    f"layout={brief.layout_template}."
                ))],
            ),
            actions=EventActions(state_delta={product_key: product_state}),
        )


creative_composer_agent = CreativeComposerAgent(name="CreativeComposerAgent")
