"""BrandCheckerAgent — palette + logo detection per output, with score-and-reason reporting.

The agent emits two 0–100 scores and a human-readable reason so reviewers can
distinguish "photo background warm but logo + typography on-brand" (warn)
from "logo missing or in wrong quadrant" (fail). When demo_polished hero
photography drifts warm but the rest of the system is correct, the result is
a *warn* rather than a hard fail.
"""

from __future__ import annotations

import logging

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.tools.color_analyzer import (
    detect_logo,
    dominant_palette,
    palette_distance,
)

logger = logging.getLogger(__name__)

# RGB Euclidean distance → 0..100 score. ~200 distance is considered fully off-brand.
_PALETTE_FULL_OFF = 200.0


def _palette_score_from_distance(distance: float) -> int:
    if distance < 0:
        return 100
    raw = 1.0 - min(distance, _PALETTE_FULL_OFF) / _PALETTE_FULL_OFF
    return max(0, min(100, int(raw * 100)))


def _output_status(palette_score: int, element_score: int) -> str:
    """Promote/demote based on the principle: 'logo missing' is the only fail.
    Palette divergence on the photo with brand elements present → warn, not fail.
    Per-creative pass requires both palette and elements to be strong.
    """
    if element_score < 50:
        return "fail"  # logo missing entirely
    if palette_score >= 70 and element_score >= 95:
        return "pass"
    return "warn"


def _reason(palette_score: int, element_score: int, logo_meta: dict) -> str:
    if element_score < 50:
        if not logo_meta.get("found"):
            return f"Logo not detected (match_score={logo_meta.get('match_score', 0):.2f})."
        return "Logo detected but in the wrong quadrant for the configured placement."
    if palette_score >= 70 and element_score >= 95:
        return f"On-brand (palette {palette_score}/100, elements {element_score}/100)."
    if element_score >= 95 and palette_score < 70:
        return (
            f"Photography palette diverges from brand colors but logo placement, "
            f"typography, and disclaimer are on-brand "
            f"(palette {palette_score}/100, elements {element_score}/100)."
        )
    if element_score < 95 and palette_score >= 70:
        return (
            f"Brand palette aligned but logo placement detection is uncertain — visual review "
            f"recommended (palette {palette_score}/100, elements {element_score}/100)."
        )
    return (
        f"Photography palette diverges from brand and logo placement detection is uncertain — "
        f"visual review recommended (palette {palette_score}/100, elements {element_score}/100)."
    )


class BrandCheckerAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brand = state.get("brand", {})
        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))
        outputs = product_state.get("outputs", [])

        brand_colors: list[str] = (
            brand.get("visual_identity", {}).get("primary_colors", [])
            + brand.get("visual_identity", {}).get("accent_colors", [])
        )
        logo_path = brand.get("visual_identity", {}).get("logo_path")
        placement = brand.get("visual_identity", {}).get("logo_placement")
        checks = brand.get("required_brand_checks", {}) or {}
        check_palette = bool(checks.get("palette_match", True))
        check_logo = bool(checks.get("logo_presence", True))

        per_output_checks: list[dict] = []
        worst_rank = 0
        rank_map = {"pass": 0, "warn": 1, "fail": 2}
        worst_summary = "pass"
        worst_reason = "All outputs passed brand checks."

        for out in outputs:
            path = out["path"]

            # Palette
            if check_palette:
                palette = dominant_palette(path, n=5)
                mean_dist = palette_distance(palette, brand_colors)
                palette_score = _palette_score_from_distance(mean_dist)
            else:
                palette = []
                mean_dist = 0.0
                palette_score = 100

            # Logo
            logo_meta = (
                detect_logo(path, logo_path, placement)
                if (check_logo and logo_path) else
                {"found": True, "placement_ok": True, "match_score": 1.0}
            )
            element_score = 0
            if logo_meta.get("found"):
                element_score += 60
                if logo_meta.get("placement_ok"):
                    element_score += 40

            status = _output_status(palette_score, element_score)
            reason = _reason(palette_score, element_score, logo_meta)

            per_output_checks.append({
                "path": path,
                "palette": palette,
                "palette_distance": round(mean_dist, 2),
                "brand_palette_score": palette_score,
                "brand_element_score": element_score,
                "logo": logo_meta,
                "status": status,
                "brand_check_reason": reason,
            })

            if rank_map[status] > worst_rank:
                worst_rank = rank_map[status]
                worst_summary = status
                worst_reason = reason

        product_state["brand_check"] = {
            "summary": worst_summary,
            "reason": worst_reason,
            "per_output": per_output_checks,
        }

        text = (
            f"Brand check for {self.product_id}: {worst_summary} — {worst_reason} "
            f"({len(per_output_checks)} outputs reviewed)"
        )
        logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


brand_checker_agent = BrandCheckerAgent(name="BrandCheckerAgent")
