"""CreativeComposerAgent — produces every aspect ratio × every (market, locale) per product.

Localization is **explicit** and driven by the brief. Three modes, in
precedence order:

  1. ``output_locales`` set (e.g. ``["en","es","pt"]``): the composer fans
     out as ``markets × output_locales``. Each (market, locale) produces
     one file. The brief author controls cardinality via the ``markets``
     list — e.g. ``markets=["US"]`` × ``output_locales=["en","es","pt"]``
     = 3 files per ratio per product, not 9. Headlines come from
     ``campaign_message_localized[locale]``; disclaimers from
     ``disclaimer_text_localized[locale]`` (with per-market and brand-level
     fallbacks below). This mode decouples *distribution market* from
     *rendered language*.
  2. ``localized_copy=True`` (without ``output_locales``): per-market locale
     picked via ``brand.market_locales``; headline pulled from
     ``brief.campaign_message_localized[locale]`` (with per-product override
     taking precedence when set).
  3. ``localized_copy=False`` (default): every (market, ratio) renders the
     same primary ``brief.campaign_message`` in ``brief.language``. No
     translation, no inference. Output filenames use ``{market}_{language}``.

The image-gen prompt cue stays tied to ``brief.language`` regardless of
which localization mode is active — one source hero per product, shared
across every (market, locale) output. Per-locale hero generation is
intentionally not implemented (would multiply Imagen API cost by
``len(output_locales)``).
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
            # Resolve the locale list to fan out for this market. Three
            # branches in precedence order:
            #   1. brief.output_locales — explicit per-locale fan-out
            #   2. brief.localized_copy — per-market locale via brand.market_locales
            #   3. neither — single-language using brief.language
            if brief.output_locales:
                locales_for_market = list(brief.output_locales)
            elif brief.localized_copy:
                available_locales = list(brief.campaign_message_localized.keys())
                locales_for_market = [
                    pick_locale(
                        market=market,
                        available_locales=available_locales,
                        market_locales=brand.market_locales,
                        fallback_language=brief.language,
                    )
                ]
            else:
                locales_for_market = [brief.language]

            for locale in locales_for_market:
                # Headline resolution (most specific wins):
                #   1. product.campaign_message                            (per-product override)
                #   2. brief.campaign_message_localized[locale]            (per-locale entry)
                #   3. brief.campaign_message                              (fallback)
                headline = (
                    product.campaign_message
                    or brief.campaign_message_localized.get(locale)
                    or brief.campaign_message
                )

                # Disclaimer resolution order (most specific wins):
                #   1. brief.disclaimer_text_localized[locale]            (per-locale, when localized_legal_copy=True)
                #   2. brief.disclaimer_text_localized[market]            (per-market, when localized_legal_copy=True)
                #   3. brief.disclaimer_text_localized[brief.language]    (fallback localized)
                #   4. brand.legal.required_disclaimers[market]           (when localized_legal_copy=True)
                #   5. brief.disclaimer_text                              (single-language brief override)
                #   6. brand.legal.default_disclaimer                     (compliance boilerplate)
                #
                # Step 1 (per-locale lookup) is what makes US_es.png get a
                # Spanish disclaimer when ``output_locales`` is in play —
                # without it, ``required_disclaimers`` (keyed by market)
                # would yield the same string for every locale variant.
                disclaimer = None
                if brief.localized_legal_copy:
                    disclaimer = (
                        brief.disclaimer_text_localized.get(locale)
                        or brief.disclaimer_text_localized.get(market)
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
                        "composed %s/%s %s_%s in %dms (localized_copy=%s, localized_legal=%s, output_locales=%s)",
                        self.product_id, ratio, market, locale, meta["duration_ms"],
                        brief.localized_copy, brief.localized_legal_copy,
                        bool(brief.output_locales),
                    )

        product_state["outputs"] = outputs
        product_state["layout_template"] = brief.layout_template
        product_state["language"] = brief.language
        product_state["localized_copy"] = brief.localized_copy
        product_state["localized_legal_copy"] = brief.localized_legal_copy
        product_state["output_locales"] = list(brief.output_locales) if brief.output_locales else None
        product_state["creative_quality"] = brief.creative_quality.value

        if brief.output_locales:
            shape = (
                f"{len(brief.markets)} markets × {len(brief.output_locales)} locales × "
                f"{len(ratios)} ratios"
            )
        else:
            shape = f"{len(brief.markets)} markets × {len(ratios)} ratios"

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Composed {len(outputs)} creatives for {self.product_id} "
                    f"({shape}) — language={brief.language}, "
                    f"localized_copy={brief.localized_copy}, "
                    f"output_locales={list(brief.output_locales) if brief.output_locales else None}, "
                    f"layout={brief.layout_template}."
                ))],
            ),
            actions=EventActions(state_delta={product_key: product_state}),
        )


creative_composer_agent = CreativeComposerAgent(name="CreativeComposerAgent")
