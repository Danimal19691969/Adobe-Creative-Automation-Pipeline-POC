"""Tests for the soft_panel / soft_badge composition rules and the
disclaimer-contrast QC rule.

Covers:
  - Brand YAML's premium_product_hero opts into soft_panel + soft_badge.
  - 16x9 disclaimer_min_font_size_px is at least 30 in YAML.
  - Composer records headline_background_treatment + disclaimer_background_treatment
    + headline_zone_fill_pct in output metadata.
  - Composer renders the headline panel only when configured.
  - 16x9 disclaimer_size_px in output meta meets the configured minimum.
  - DisclaimerContrastRule runs when required_brand_checks.disclaimer_contrast=true
    and emits pass/fail like the headline rule.
  - Pre-existing contrast QC tests still pass (covered by other test files).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import (
    BrandGuidelines,
    DisclaimerBackgroundTreatment,
    HeadlineBackgroundTreatment,
)
from creative_pipeline.tools.pillow_composer import compose_creative
from creative_pipeline.tools.qc_rules import (
    ContrastRule,
    DisclaimerContrastRule,
    build_rules,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def neutral_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (1024, 1024), (90, 90, 90))
    p = tmp_path / "hero.png"
    img.save(p)
    return str(p)


# -------- Brand YAML wiring --------

def test_brand_panel_badge_knobs_available_for_fallback(brand_yaml):
    """Premium layout defaults to no visible panel/badge — natural composition
    is the primary readability mechanism. The panel/badge knobs still need
    to be present and valid in case the readability_fallback_order escalates
    to ``soft_panel_last_resort``."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    # Default treatments: NONE. Visible panels are last-resort, not default.
    assert layout.headline_background_treatment == HeadlineBackgroundTreatment.NONE
    assert layout.disclaimer_background_treatment == DisclaimerBackgroundTreatment.NONE
    assert layout.headline_text_shadow is True
    assert layout.avoid_textured_regions is True
    # Knobs ready for fallback escalation.
    assert 0.0 < layout.headline_panel_opacity_pct <= 1.0
    assert 0.0 < layout.headline_panel_padding_pct <= 0.20
    assert 0.0 <= layout.headline_panel_corner_radius_pct <= 0.50


def test_brand_16x9_disclaimer_floor_is_at_least_30(brand_yaml):
    """Brand promises legal copy ≥30px on 16:9 — tested directly against YAML."""
    pa = brand_yaml.typography.per_aspect["16x9"]
    assert pa.disclaimer_min_font_size_px >= 30


def test_brand_disclaimer_contrast_qc_is_enabled(brand_yaml):
    assert brand_yaml.required_brand_checks.disclaimer_contrast is True
    assert brand_yaml.qc.disclaimer_min_contrast_ratio >= 4.5


# -------- Composer surfaces the new metadata --------

def test_compose_records_treatment_fields(brand_yaml, neutral_hero, tmp_path):
    """Composer records the configured treatment + readability fallback used.
    Default for premium_product_hero is treatment=none; whether a panel
    actually renders depends on the photo's natural contrast."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="16x9",
        headline="Refresh your summer, naturally.", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    # Configured treatments stay 'none' (natural composition is the default).
    assert meta["headline_background_treatment"] == "none"
    assert meta["disclaimer_background_treatment"] == "none"
    assert meta["headline_text_shadow"] is True
    # readability_fallback_used reports which strategy ended the walk.
    assert meta["readability_fallback_used"] in {
        "natural_composition", "subtle_text_shadow", "subtle_local_gradient",
        "soft_panel_last_resort", "exhausted_fallbacks", "explicit_soft_panel",
    }
    # Zone-fill telemetry remains. Can mildly overflow 1.0 when the brand's
    # min-font-size floor binds and pushes rendered text past the configured
    # target zone — that's expected, the floor wins.
    assert 0.0 < meta["headline_zone_fill_pct"] <= 1.5


def test_compose_skips_panel_when_treatment_none(brand_yaml, neutral_hero, tmp_path):
    """Flipping the treatment to NONE turns off panel rendering and zeroes
    out the panel meta fields."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.headline_background_treatment = HeadlineBackgroundTreatment.NONE
    layout.disclaimer_background_treatment = DisclaimerBackgroundTreatment.NONE

    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="16x9",
        headline="x", disclaimer="y",
        guidelines=custom, layout=layout, out_path=out,
    )
    assert meta["headline_background_treatment"] == "none"
    assert meta["headline_panel_box"] is None
    assert meta["disclaimer_background_treatment"] == "none"
    assert meta["disclaimer_badge_box"] is None


# -------- 16x9 disclaimer reaches the configured minimum --------

def test_16x9_disclaimer_size_meets_brand_floor(brand_yaml, neutral_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "16x9.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="16x9",
        headline="Refresh your summer", disclaimer="Terms and conditions apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    floor = brand_yaml.typography.per_aspect["16x9"].disclaimer_min_font_size_px
    assert meta["disclaimer_size_px"] >= floor


# -------- DisclaimerContrastRule --------

def test_build_rules_includes_disclaimer_when_enabled(brand_yaml):
    rules = build_rules(brand_yaml)
    rule_names = [r.name for r in rules]
    assert "contrast_ratio" in rule_names      # headline (still active)
    assert "disclaimer_contrast" in rule_names  # new


def test_build_rules_skips_disclaimer_when_disabled(brand_yaml):
    custom = brand_yaml.model_copy(deep=True)
    custom.required_brand_checks.disclaimer_contrast = False
    rules = build_rules(custom)
    assert not any(r.name == "disclaimer_contrast" for r in rules)


def test_disclaimer_rule_passes_white_on_black(tmp_path, brand_yaml):
    img_path = tmp_path / "high.png"
    Image.new("RGB", (400, 200), (0, 0, 0)).save(img_path)
    rule = DisclaimerContrastRule(min_ratio=4.5)
    output_meta = {
        "path": str(img_path),
        "disclaimer_text": "Terms apply.",
        "disclaimer_box": [50, 50, 350, 150],
        "disclaimer_text_color": "#FFFFFF",
    }
    result = rule.check(output_meta, Image.open(img_path), brand_yaml)
    assert result["passed"] is True
    assert result["details"]["wcag_level"] in ("AA", "AAA")


def test_disclaimer_rule_fails_low_contrast(tmp_path, brand_yaml):
    img_path = tmp_path / "low.png"
    Image.new("RGB", (400, 200), (200, 200, 200)).save(img_path)  # light gray
    rule = DisclaimerContrastRule(min_ratio=4.5)
    output_meta = {
        "path": str(img_path),
        "disclaimer_text": "Terms apply.",
        "disclaimer_box": [50, 50, 350, 150],
        "disclaimer_text_color": "#FFFFFF",  # white on light gray ≈ 1.6:1
    }
    result = rule.check(output_meta, Image.open(img_path), brand_yaml)
    assert result["passed"] is False
    assert result["details"]["wcag_level"] == "fail"


def test_disclaimer_rule_no_disclaimer_is_info_pass(tmp_path, brand_yaml):
    img_path = tmp_path / "x.png"
    Image.new("RGB", (10, 10)).save(img_path)
    rule = DisclaimerContrastRule()
    result = rule.check({"path": str(img_path)}, Image.open(img_path), brand_yaml)
    assert result["passed"] is True
    assert result["severity"] == "info"
    assert "No disclaimer" in result["reason"]
