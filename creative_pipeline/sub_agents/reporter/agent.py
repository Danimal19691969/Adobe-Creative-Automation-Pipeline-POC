"""ReportingAgent — aggregates session state into outputs/report_{ISO8601}.json."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.tools.storage_adapter import LocalStorageAdapter

logger = logging.getLogger(__name__)

_RUN_STARTED_KEY = "run_started_at"


def _started_at_or_now(state: dict) -> str:
    started = state.get(_RUN_STARTED_KEY)
    if started:
        return started
    return datetime.now(timezone.utc).isoformat()


def _derive_source_origin(product_state: dict) -> str:
    """Unambiguous label for where the source hero came from this run.

    Possible values (from existing per-product state, no new state shape):
      - "generated_this_run"            — ImageGeneratorAgent fired the API this run
      - "reused_generated_previous_run" — AssetManager loaded a prior outputs/{pid}/source/* file
      - "reused_local_placeholder"      — AssetManager loaded inputs/assets/{pid}/hero.*
      - "no_source"                     — neither path produced a hero (failure mode)
    """
    asset_source = product_state.get("asset_source")
    used_cache = product_state.get("used_cache")
    if asset_source == "user_supplied":
        return "reused_local_placeholder"
    if asset_source in ("openai_generated", "imagen_generated"):
        return "reused_generated_previous_run" if used_cache else "generated_this_run"
    if asset_source == "generated_cached":
        return "reused_generated_previous_run"
    if asset_source is None and product_state.get("hero_path") is None:
        return "no_source"
    return "unknown"


class ReportingAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brief = state.get("brief", {}) or {}
        product_ids: list[str] = state.get("product_ids", [])

        completed_at = datetime.now(timezone.utc)
        started_at_iso = _started_at_or_now(state)
        try:
            duration_ms = int(
                (completed_at - datetime.fromisoformat(started_at_iso)).total_seconds() * 1000
            )
        except ValueError:
            duration_ms = 0

        # Provider env snapshot — captured at report time, not run-start time.
        provider_env = os.environ.get("PROVIDER", "<unset>")
        model_env = os.environ.get("MODEL", "<unset>")
        image_provider_env = os.environ.get("IMAGE_PROVIDER") or provider_env

        products_report: list[dict] = []
        for pid in product_ids:
            ps = state.get(f"product:{pid}", {}) or {}
            outputs = ps.get("outputs", [])
            brand_check = ps.get("brand_check", {}) or {}
            legal_check = ps.get("legal_check", {}) or {}
            per_brand = {item["path"]: item for item in brand_check.get("per_output", [])}
            qc_check = ps.get("qc_check", {}) or {}
            source_origin = _derive_source_origin(ps)

            output_records = []
            for o in outputs:
                bc = per_brand.get(o.get("path"), {})
                qc = o.get("qc_check", {}) or {}
                output_records.append({
                    "market": o.get("market"),
                    "locale": o.get("locale"),
                    "ratio": o.get("ratio"),
                    "path": o.get("path"),
                    "headline": o.get("headline"),
                    "disclaimer_text": o.get("disclaimer_text"),
                    # Per-output provenance, mirrored from product-level state so each
                    # output row is self-contained for downstream consumers.
                    "source_asset_path": ps.get("hero_path"),
                    "source_origin": source_origin,
                    "asset_source": ps.get("asset_source"),
                    "image_provider": ps.get("image_provider"),
                    "image_model": ps.get("image_model"),
                    "used_cache": ps.get("used_cache"),
                    "force_generate_hero": brief.get("force_generate_hero"),
                    "regenerate_cached_assets": brief.get("regenerate_cached_assets"),
                    "language": brief.get("language"),
                    "localized_copy": brief.get("localized_copy"),
                    "localized_legal_copy": brief.get("localized_legal_copy"),
                    "layout_template": brief.get("layout_template"),
                    "creative_quality": brief.get("creative_quality"),
                    # Layout accounting from the composer.
                    "headline_box": o.get("headline_box"),
                    "disclaimer_box": o.get("disclaimer_box"),
                    "logo_box": o.get("logo_box"),
                    "logo_position": o.get("logo_position"),
                    "logo_size_px": o.get("logo_size_px"),
                    "logo_treatment": o.get("logo_treatment"),
                    "overlay_style": o.get("overlay_style"),
                    "overlay_opacity": o.get("overlay_opacity"),
                    "accent_style": o.get("accent_style"),
                    "accent_color": o.get("accent_color"),
                    # Final rendered type sizes — driven by brand.typography.per_aspect.
                    "headline_size_px": o.get("headline_size_px"),
                    "headline_line_count": o.get("headline_line_count"),
                    "disclaimer_size_px": o.get("disclaimer_size_px"),
                    # Local readability treatments (panel behind headline,
                    # badge behind disclaimer) and headline zone-fill telemetry.
                    "headline_background_treatment": o.get("headline_background_treatment"),
                    "headline_text_shadow": o.get("headline_text_shadow"),
                    "headline_zone_fill_pct": o.get("headline_zone_fill_pct"),
                    "disclaimer_background_treatment": o.get("disclaimer_background_treatment"),
                    # Photographic-composition + fallback telemetry.
                    "image_composition_guidance_used": o.get("image_composition_guidance_used"),
                    "negative_space_location": o.get("negative_space_location"),
                    "readability_fallback_used": o.get("readability_fallback_used"),
                    "composer_contrast_estimate": o.get("composer_contrast_estimate"),
                    "post_treatment_contrast_estimate": o.get("post_treatment_contrast_estimate"),
                    # Headline-zone selection audit (text-safe-area scoring).
                    "headline_box_selected": o.get("headline_box_selected_pct"),
                    "headline_box_selected_pct": o.get("headline_box_selected_pct"),
                    "headline_box_selected_px": o.get("headline_box_selected_px"),
                    "headline_selection_reason": o.get("headline_box_selection_reason"),
                    "headline_box_selection_reason": o.get("headline_box_selection_reason"),
                    "headline_box_score": o.get("headline_box_score"),
                    "headline_box_scores": o.get("headline_box_candidates"),
                    "headline_region_texture_score": o.get("headline_region_texture_score"),
                    "headline_region_edge_density": o.get("headline_region_edge_density"),
                    "headline_region_contrast_estimate": o.get("headline_region_contrast_estimate"),
                    "headline_color_selected": o.get("headline_color_selected"),
                    "headline_wrap_variant": o.get("headline_wrap_variant"),
                    "headline_scale_reason": o.get("headline_scale_reason"),
                    "headline_fit_status": o.get("headline_fit_status"),
                    # Prominence score (chosen size / configured ceiling).
                    "headline_prominence_score": o.get("headline_prominence_score"),
                    "headline_max_size_px_configured": o.get("headline_max_size_px_configured"),
                    "headline_min_size_px_configured": o.get("headline_min_size_px_configured"),
                    "headline_target_h_px": o.get("headline_target_h_px"),
                    "headline_box_candidates": o.get("headline_box_candidates"),
                    # Focal-area / product safe-zone audit.
                    "focal_area_estimate": o.get("focal_area_estimate"),
                    "product_safe_zone_box": o.get("product_safe_zone_box"),
                    "expanded_product_safe_zone_box": o.get("expanded_product_safe_zone_box"),
                    "focal_overlap_detected": o.get("focal_overlap_detected"),
                    "focal_near_miss_detected": o.get("focal_near_miss_detected"),
                    "focal_overlap_pct": o.get("focal_overlap_pct"),
                    "text_object_gap_px": o.get("text_object_gap_px"),
                    "text_object_clearance_pass": o.get("text_object_clearance_pass"),
                    # Headline-box adjustment audit.
                    "headline_box_original": o.get("headline_box_original"),
                    "headline_box_adjusted": o.get("headline_box_adjusted"),
                    "headline_box_adjustment_reason": o.get("headline_box_adjustment_reason"),
                    # Disclaimer clearance audit.
                    "disclaimer_text_object_gap_px": o.get("disclaimer_text_object_gap_px"),
                    "disclaimer_clearance_pass": o.get("disclaimer_clearance_pass"),
                    # Disclaimer-contrast QC (set when required_brand_checks.disclaimer_contrast is true).
                    "disclaimer_contrast_ratio": qc.get("disclaimer_contrast_ratio"),
                    "disclaimer_wcag_level": qc.get("disclaimer_wcag_level"),
                    "disclaimer_background_sample_color": qc.get("disclaimer_background_color"),
                    # Brand check, with scores + reason.
                    "brand_check": bc.get("status", "n/a"),
                    "brand_check_reason": bc.get("brand_check_reason"),
                    "brand_palette_score": bc.get("brand_palette_score"),
                    "brand_element_score": bc.get("brand_element_score"),
                    "legal_check": legal_check.get("summary", "n/a"),
                    # QC (WCAG contrast and any future modular rules).
                    "qc_check": qc.get("summary", "n/a"),
                    "contrast_ratio": qc.get("contrast_ratio"),
                    "wcag_level": qc.get("wcag_level"),
                    "text_color": qc.get("text_color") or o.get("text_color_used"),
                    "background_color": qc.get("background_color"),
                    "qc_rules": qc.get("rules"),
                })

            products_report.append({
                "product_id": pid,
                "asset_source": ps.get("asset_source"),
                "source_asset_path": ps.get("hero_path"),
                # Unambiguous label: generated_this_run | reused_generated_previous_run
                # | reused_local_placeholder | no_source.
                "source_origin": source_origin,
                "image_provider": ps.get("image_provider"),
                "image_model": ps.get("image_model"),
                "image_gen_latency_ms": ps.get("image_gen_latency_ms"),
                "used_cache": ps.get("used_cache"),
                "outputs": output_records,
                "brand_check_summary": brand_check.get("summary", "n/a"),
                "legal_check_summary": legal_check.get("summary", "n/a"),
                "qc_check_summary": qc_check.get("summary", "n/a"),
                "qc_failures": qc_check.get("failures", []),
                "qc_rules_run": qc_check.get("rules_run", []),
                "warnings": ps.get("warnings", []),
            })

        report = {
            "campaign_id": brief.get("campaign_id"),
            "campaign_name": brief.get("campaign_name"),
            "brand_id": brief.get("brand_id"),
            "started_at": started_at_iso,
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "language": brief.get("language"),
            "localized_copy": brief.get("localized_copy"),
            "localized_legal_copy": brief.get("localized_legal_copy"),
            "force_generate_hero": brief.get("force_generate_hero"),
            "regenerate_cached_assets": brief.get("regenerate_cached_assets"),
            "creative_quality": brief.get("creative_quality"),
            "layout_template": brief.get("layout_template"),
            "provider_env": provider_env,
            "model_env": model_env,
            "image_provider_env": image_provider_env,
            "products": products_report,
        }

        timestamp = completed_at.strftime("%Y%m%dT%H%M%SZ")
        output_dir = os.environ.get("OUTPUT_DIR", "outputs")
        report_path = f"{output_dir}/report_{timestamp}.json"
        LocalStorageAdapter().write(
            report_path, json.dumps(report, indent=2).encode("utf-8")
        )
        logger.info("Wrote report: %s", report_path)

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Wrote run report → {report_path}")],
            ),
            actions=EventActions(state_delta={"report_path": report_path}),
        )


reporter_agent = ReportingAgent(name="ReportingAgent")
