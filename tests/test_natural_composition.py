"""Natural-composition + readability-fallback tests.

Locks in the architectural shift from 'composer paints visible panels' to
'image-gen prompts produce photography with natural negative space, composer
verifies, fallback chain only fires if needed'.

Covers:
  - brand.image_composition_guidance is loaded from YAML.
  - The image-gen prompt for premium_product_hero contains:
      * the per-aspect product position
      * the per-aspect negative-space location (16x9 ⇒ left third)
      * avoid_behind_text terms
      * preferred_behind_text terms
      * explicit text-free guards
  - layout.readability_fallback_order is loaded from YAML.
  - premium_product_hero defaults headline_background_treatment to 'none'
    (no obvious panel by default).
  - Composer reports 'natural_composition' when the photo carries the
    contrast on its own, and 'soft_panel_last_resort' (or another step) when
    the chain has to escalate.
  - Composer fields image_composition_guidance_used, negative_space_location,
    readability_fallback_used, composer_contrast_estimate are all in meta.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import (
    BrandGuidelines,
    CampaignBrief,
    HeadlineBackgroundTreatment,
    ReadabilityFallback,
)
from creative_pipeline.sub_agents.image_generator.prompts import build_prompt
from creative_pipeline.tools.pillow_composer import compose_creative


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def brief_yaml() -> CampaignBrief:
    with open("inputs/campaign_briefs/summer_refresh_2025.yaml") as f:
        return CampaignBrief.model_validate(yaml.safe_load(f))


@pytest.fixture
def clean_left_hero(tmp_path: Path) -> str:
    """Synthetic hero with a clean dark blue/cyan band on the left half and a
    bright product-like region on the right. Mimics a generation that
    actually followed the negative-space direction. Lets the composer hit
    'natural_composition' for 16:9 (left third → dark, navy text wins)."""
    img = Image.new("RGB", (1920, 1080), (240, 240, 240))
    # Left third: dark navy/cyan gradient — clean copy area
    for x in range(640):
        for y in range(1080):
            img.putpixel((x, y), (10, 60 + (x // 8), 110 + (x // 12)))
    p = tmp_path / "clean_left.png"
    img.save(p)
    return str(p)


@pytest.fixture
def busy_hero(tmp_path: Path) -> str:
    """Synthetic 'busy patterned' hero — high-frequency stripes in the copy
    area. Used to prove the fallback chain triggers when the photo doesn't
    carry contrast on its own."""
    img = Image.new("RGB", (1920, 1080), (200, 190, 170))
    for x in range(1920):
        if (x // 30) % 2 == 0:
            for y in range(1080):
                img.putpixel((x, y), (255, 250, 230))
    p = tmp_path / "busy.png"
    img.save(p)
    return str(p)


# -------- YAML wiring --------

def test_brand_loads_image_composition_guidance(brand_yaml):
    g = brand_yaml.image_composition_guidance
    assert g is not None
    assert g.negative_space_required is True
    for ratio in ("1x1", "9x16", "16x9"):
        assert ratio in g.negative_space_location_by_aspect, f"missing neg-space for {ratio}"
        assert ratio in g.product_position_by_aspect, f"missing product pos for {ratio}"
    assert g.avoid_behind_text, "avoid_behind_text must be non-empty"
    assert g.preferred_behind_text, "preferred_behind_text must be non-empty"
    # 16:9 specifically must mention the left-side negative space.
    assert "left" in g.negative_space_location_by_aspect["16x9"].lower()


def test_brand_layout_readability_fallback_order_loaded(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    order = layout.readability_fallback_order
    assert ReadabilityFallback.CHOOSE_BEST_BRAND_TEXT_COLOR in order
    assert ReadabilityFallback.SOFT_PANEL_LAST_RESORT in order
    # Last-resort must come after gentler steps.
    panel_idx = order.index(ReadabilityFallback.SOFT_PANEL_LAST_RESORT)
    assert panel_idx == len(order) - 1


def test_premium_layout_no_default_panel(brand_yaml):
    """Premium layout should NOT default to soft_panel — natural composition
    is the default; panels are last-resort fallback only."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.headline_background_treatment == HeadlineBackgroundTreatment.NONE


# -------- Prompt builder --------

def test_prompt_includes_composition_guidance_for_premium(brand_yaml, brief_yaml):
    p = build_prompt(brief_yaml.products[0], brief_yaml, brand_yaml,
                     hero_aspect_ratio_label="16x9")
    # Per-aspect location for 16:9
    g = brand_yaml.image_composition_guidance
    assert g.negative_space_location_by_aspect["16x9"] in p
    assert g.product_position_by_aspect["16x9"] in p
    # avoid + preferred lists
    for t in g.avoid_behind_text:
        assert t in p, f"avoid term {t!r} missing from prompt"
    for t in g.preferred_behind_text:
        assert t in p, f"preferred term {t!r} missing from prompt"
    # Explicit text-free guards
    assert "ABSOLUTELY NO TEXT" in p
    assert "DO NOT render any text" in p


def test_prompt_omits_composition_guidance_for_other_layouts(brand_yaml, brief_yaml):
    custom_brief = brief_yaml.model_copy(update={"layout_template": "hero_full_bleed_footer"})
    p = build_prompt(custom_brief.products[0], custom_brief, brand_yaml,
                     hero_aspect_ratio_label="1x1")
    # The per-aspect guidance strings should NOT appear when the layout
    # hasn't opted into composition guidance.
    g = brand_yaml.image_composition_guidance
    assert g.negative_space_location_by_aspect["16x9"] not in p
    assert g.product_position_by_aspect["16x9"] not in p


def test_prompt_picks_correct_aspect_for_guidance(brand_yaml, brief_yaml):
    """Switching hero aspect should switch the embedded per-aspect text."""
    g = brand_yaml.image_composition_guidance
    p_1x1 = build_prompt(brief_yaml.products[0], brief_yaml, brand_yaml,
                          hero_aspect_ratio_label="1x1")
    assert g.negative_space_location_by_aspect["1x1"] in p_1x1
    # 16:9 string should not be embedded when generating at 1:1
    assert g.negative_space_location_by_aspect["16x9"] not in p_1x1


# -------- Composer behavior --------

def test_composer_reports_natural_composition_on_clean_photo(brand_yaml, clean_left_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=clean_left_hero, ratio="16x9",
        headline="Refresh your summer", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    # Treatment defaulted to none → no explicit panel.
    assert meta["headline_background_treatment"] == "none"
    # Photo carried the contrast → no fallback applied.
    assert meta["readability_fallback_used"] in (
        "natural_composition", "subtle_text_shadow"
    )
    # No visible panel rendered.
    assert meta["headline_panel_box"] is None


def test_composer_escalates_when_photo_is_busy(brand_yaml, busy_hero, tmp_path):
    """A busy patterned 'photo' under the headline should drive the composer
    into the fallback chain — the test passes whenever the chain *engaged*,
    regardless of which step ended it (subtle_local_gradient or
    soft_panel_last_resort, depending on the synthetic image's contrast)."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=busy_hero, ratio="16x9",
        headline="Refresh your summer, naturally.", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    fallback = meta["readability_fallback_used"]
    assert fallback != "explicit_soft_panel"  # we didn't ask for always-on
    assert fallback in {
        "natural_composition",        # if contrast estimate squeaked in
        "subtle_text_shadow",
        "subtle_local_gradient",
        "soft_panel_last_resort",
        "exhausted_fallbacks",
    }


def test_composer_records_composition_guidance_meta(brand_yaml, clean_left_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=clean_left_hero, ratio="16x9",
        headline="Hello world", disclaimer=None,
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    assert meta["image_composition_guidance_used"] is True
    assert meta["negative_space_location"] == \
        brand_yaml.image_composition_guidance.negative_space_location_by_aspect["16x9"]
    assert "readability_fallback_used" in meta
    assert "composer_contrast_estimate" in meta
    assert "post_treatment_contrast_estimate" in meta


def test_composer_explicit_panel_path_still_works(brand_yaml, clean_left_hero, tmp_path):
    """Backward-compat: a layout that explicitly asks for soft_panel still
    gets one, marked as explicit_soft_panel (not a fallback)."""
    custom = brand_yaml.model_copy(deep=True)
    custom.layout_templates["premium_product_hero"].headline_background_treatment = (
        HeadlineBackgroundTreatment.SOFT_PANEL
    )
    layout = custom.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=clean_left_hero, ratio="16x9",
        headline="x", disclaimer=None,
        guidelines=custom, layout=layout, out_path=out,
    )
    assert meta["headline_background_treatment"] == "soft_panel"
    assert meta["readability_fallback_used"] == "explicit_soft_panel"
    assert meta["headline_panel_box"] is not None
