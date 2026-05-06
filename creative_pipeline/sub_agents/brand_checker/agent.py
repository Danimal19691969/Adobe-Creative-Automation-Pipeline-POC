"""BrandCheckerAgent — palette match + logo presence/quadrant checks per output."""

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

# RGB Euclidean threshold; ~80 is a permissive "in-family" match for the PoC.
_PALETTE_PASS = 80.0
_PALETTE_WARN = 130.0


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

        per_output_checks: list[dict] = []
        worst = "pass"
        for out in outputs:
            path = out["path"]
            palette = dominant_palette(path, n=5)
            mean_dist = palette_distance(palette, brand_colors)
            if mean_dist <= _PALETTE_PASS:
                palette_status = "pass"
            elif mean_dist <= _PALETTE_WARN:
                palette_status = "warn"
            else:
                palette_status = "fail"

            logo = detect_logo(path, logo_path, placement) if logo_path else {"found": False, "placement_ok": False, "match_score": 0.0}

            if logo["found"] and logo["placement_ok"]:
                logo_status = "pass"
            elif logo["found"]:
                logo_status = "warn"  # found but in wrong quadrant
            else:
                logo_status = "fail"

            output_status = (
                "pass" if palette_status == "pass" and logo_status == "pass"
                else "fail" if "fail" in (palette_status, logo_status)
                else "warn"
            )

            per_output_checks.append({
                "path": path,
                "palette": palette,
                "palette_distance": round(mean_dist, 2),
                "palette_status": palette_status,
                "logo": logo,
                "logo_status": logo_status,
                "status": output_status,
            })

            # Roll-up: worst wins
            ranking = {"pass": 0, "warn": 1, "fail": 2}
            if ranking[output_status] > ranking[worst]:
                worst = output_status

        product_state["brand_check"] = {"summary": worst, "per_output": per_output_checks}

        text = (
            f"Brand check for {self.product_id}: {worst} "
            f"({len(per_output_checks)} outputs reviewed)"
        )
        logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


brand_checker_agent = BrandCheckerAgent(name="BrandCheckerAgent")
