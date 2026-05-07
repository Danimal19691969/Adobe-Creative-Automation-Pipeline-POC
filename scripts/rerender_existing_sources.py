"""Rerender every output using the cached source heroes already in
``outputs/{product}/source/``. Skips image generation entirely so we can
audit composition changes deterministically.

Run from repo root:
    .venv/bin/python scripts/rerender_existing_sources.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.tools.file_utils import output_path, pick_locale
from creative_pipeline.tools.pillow_composer import compose_creative


def _latest_source_for(product_id: str) -> Path | None:
    """Prefer the most recent generated source under
    ``outputs/{pid}/source/``; fall back to the local
    ``inputs/assets/{pid}/hero.png`` placeholder when no generated
    source has been cached yet."""
    src_dir = REPO / "outputs" / product_id / "source"
    if src_dir.exists():
        candidates = sorted(src_dir.glob("*.png"))
        if candidates:
            return candidates[-1]
    local_hero = REPO / "inputs" / "assets" / product_id / "hero.png"
    if local_hero.exists():
        return local_hero
    return None


def main() -> None:
    brand = BrandGuidelines.model_validate(
        yaml.safe_load((REPO / "inputs/brand/guidelines.yaml").read_text())
    )
    brief = CampaignBrief.model_validate(
        yaml.safe_load((REPO / "inputs/campaign_briefs/summer_refresh_2025.yaml").read_text())
    )

    layout = brand.layout_templates[brief.layout_template]
    output_dir = REPO / "outputs"

    summary: list[dict] = []

    for product in brief.products:
        hero = _latest_source_for(product.id)
        if hero is None:
            print(f"!! no source hero for {product.id} — skipping")
            continue
        print(f"=== {product.id} (hero {hero.name}) ===")

        for market in brief.markets:
            if brief.localized_copy:
                locale = pick_locale(
                    market=market,
                    available_locales=list(brief.campaign_message_localized.keys()),
                    market_locales=brand.market_locales,
                    fallback_language=brief.language,
                )
                headline = (
                    product.campaign_message
                    or brief.campaign_message_localized.get(locale)
                    or brief.campaign_message
                )
            else:
                locale = brief.language
                headline = product.campaign_message or brief.campaign_message

            disclaimer = (
                brand.legal.required_disclaimers.get(market) or brand.legal.default_disclaimer
                if brief.localized_legal_copy else brand.legal.default_disclaimer
            )

            for ratio in brand.aspect_ratios.keys():
                out = output_path(str(output_dir), product.id, ratio, market, locale)
                meta = compose_creative(
                    hero_path=str(hero), ratio=ratio,
                    headline=headline, disclaimer=disclaimer,
                    guidelines=brand, layout=layout, out_path=out,
                )
                summary.append({
                    "product": product.id,
                    "ratio": ratio,
                    "market": market,
                    "path": meta["path"],
                    "headline_size_px": meta["headline_size_px"],
                    "headline_line_count": meta["headline_line_count"],
                    "headline_zone_fill_pct": meta["headline_zone_fill_pct"],
                    "headline_prominence_score": meta["headline_prominence_score"],
                    "headline_color_selected": meta["headline_color_selected"],
                    "rendered_headline_bbox": meta["rendered_headline_bbox"],
                    "headline_box_selected_pct": meta["headline_box_selected_pct"],
                    "text_object_gap_px": meta["text_object_gap_px"],
                    "text_object_clearance_pass": meta["text_object_clearance_pass"],
                    "text_object_collision_detected": meta["text_object_collision_detected"],
                    "text_object_near_miss_detected": meta["text_object_near_miss_detected"],
                    "all_candidates_failed_clearance": meta["all_candidates_failed_clearance"],
                    "headline_box_shift_attempts": len(meta.get("headline_box_shift_attempts") or []),
                    "headline_box_shift_success": meta["headline_box_shift_success"],
                    "readability_fallback_used": meta["readability_fallback_used"],
                    "post_treatment_contrast_estimate": meta["post_treatment_contrast_estimate"],
                    "headline_fit_status": meta["headline_fit_status"],
                    # New art-director pass.
                    "crop_strategy_used": meta["crop_strategy_used"],
                    "crop_box_used": meta["crop_box_used"],
                    "focal_edge_min_gap_px": meta["focal_edge_min_gap_px"],
                    "focal_edge_clearance_pass": meta["focal_edge_clearance_pass"],
                    "focal_edge_clip_detected": meta["focal_edge_clip_detected"],
                    "logo_position_selected": meta["logo_position_selected"],
                    "logo_position_configured": meta["logo_position_configured"],
                    "logo_position_adjusted": meta["logo_position_adjusted"],
                    "logo_product_gap_px": meta["logo_product_gap_px"],
                    "logo_product_clearance_pass": meta["logo_product_clearance_pass"],
                    "logo_collision_detected": meta["logo_collision_detected"],
                    "accent_edge_gap_px": meta["accent_edge_gap_px"],
                    "accent_safe_zone_pass": meta["accent_safe_zone_pass"],
                    "disclaimer_candidate_index": meta["disclaimer_candidate_index"],
                    "disclaimer_position_selected": meta["disclaimer_position_selected"],
                    "disclaimer_clearance_pass": meta["disclaimer_clearance_pass"],
                    "disclaimer_text_object_gap_px": meta["disclaimer_text_object_gap_px"],
                    "composition_score": meta["composition_score"],
                    "composition_warnings": meta["composition_warnings"],
                    # Final composition pass: wrap strategy, box widening, letterbox.
                    "headline_wrap_strategy": meta["headline_wrap_strategy"],
                    "headline_box_widened": meta["headline_box_widened"],
                    "headline_box_width_delta_pct": meta["headline_box_width_delta_pct"],
                    "headline_widen_reason": meta["headline_widen_reason"],
                    "letterbox_applied": meta["letterbox_applied"],
                    "letterbox_pad_pct": meta["letterbox_pad_pct"],
                    "letterbox_color_used": meta["letterbox_color_used"],
                    "letterbox_color_source": meta["letterbox_color_source"],
                })
                print(
                    f"  {ratio} {market}_{locale}: "
                    f"size={meta['headline_size_px']}px lines={meta['headline_line_count']} "
                    f"prominence={meta['headline_prominence_score']} "
                    f"gap={meta['text_object_gap_px']} pass={meta['text_object_clearance_pass']} "
                    f"shifts={len(meta.get('headline_box_shift_attempts') or [])} "
                    f"color={meta['headline_color_selected']}"
                )

    summary_path = REPO / "outputs" / "rerender_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print(f"\nWrote {summary_path}")


if __name__ == "__main__":
    main()
