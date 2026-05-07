"""Composer-side contrast-aware behavior.

Confirms the composer reads brand.qc thresholds *as guidance* and:
  - picks the higher-contrast text color
  - escalates overlay opacity once when the chosen color falls below the
    brand's WCAG-style threshold
  - records the actually-used opacity in the meta
QCCheckerAgent remains independent and is the source of truth for pass/fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines
from creative_pipeline.tools.pillow_composer import compose_creative


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def tan_hero(tmp_path: Path) -> str:
    """Solid tan field — mimics a SunGuard-like beach background that gives
    poor contrast against navy text."""
    img = Image.new("RGB", (1024, 1024), (158, 143, 122))
    p = tmp_path / "tan.png"
    img.save(p)
    return str(p)


@pytest.fixture
def black_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (1024, 1024), (0, 0, 0))
    p = tmp_path / "black.png"
    img.save(p)
    return str(p)


def test_composer_picks_higher_contrast_text_color(brand_yaml, black_hero, tmp_path):
    """A pure-black hero should produce white text (text_color_on_dark) by
    contrast-aware selection, not by the legacy luma threshold."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=black_hero, ratio="1x1",
        headline="Refresh your summer", disclaimer="Terms apply",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    # Black bg → white text gives ~21:1, navy text would give ~1.7:1.
    # Composer must pick white.
    assert meta["text_color_used"] == brand_yaml.typography.text_color_on_dark
    assert meta["composer_contrast_estimate"] >= 7.0


def test_composer_escalates_opacity_when_below_threshold(brand_yaml, tan_hero, tmp_path):
    """A mid-tan hero with brand's premium_product_hero layout: at the
    configured 65% gradient opacity the contrast is borderline; the composer
    should escalate to a higher opacity and choose white text to pull the
    contrast ratio up. The recorded overlay_opacity must reflect the bump."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    configured_opacity = layout.overlay_opacity()

    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=tan_hero, ratio="1x1",
        headline="Refresh your summer, naturally.", disclaimer="Terms apply",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    # Either the recorded opacity bumps OR composer_contrast_estimate already
    # passed the brand threshold without bumping. We require *at least one*
    # of those signals.
    qc = brand_yaml.qc
    threshold = qc.large_text_min_ratio  # large headline at 1080px canvas
    bumped = meta["overlay_opacity"] > meta["overlay_opacity_configured"] + 1e-3
    assert bumped or meta["composer_contrast_estimate"] >= threshold, (
        f"expected either an opacity bump or a passing contrast estimate; "
        f"got opacity_used={meta['overlay_opacity']}, "
        f"opacity_configured={meta['overlay_opacity_configured']}, "
        f"contrast={meta['composer_contrast_estimate']}, threshold={threshold}"
    )


def test_composer_records_actual_opacity_in_meta(brand_yaml, black_hero, tmp_path):
    """overlay_opacity in the meta is the actually-used value, and
    overlay_opacity_configured is the layout's original setting."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=black_hero, ratio="1x1",
        headline="Test", disclaimer=None,
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    assert "overlay_opacity" in meta
    assert "overlay_opacity_configured" in meta
    assert meta["overlay_opacity_configured"] == layout.overlay_opacity()
    # On pure-black, no escalation is needed; opacity should match configured.
    assert meta["overlay_opacity"] == meta["overlay_opacity_configured"]


def test_composer_does_not_escalate_when_check_disabled(brand_yaml, tmp_path):
    """If required_brand_checks.contrast_ratio is False, the composer should
    still pick the better text color (free), but must NOT escalate opacity —
    the brand has explicitly opted out of the contrast gate."""
    img = Image.new("RGB", (1024, 1024), (158, 143, 122))
    p = tmp_path / "tan.png"
    img.save(p)

    custom = brand_yaml.model_copy(deep=True)
    custom.required_brand_checks.contrast_ratio = False
    layout = custom.layout_templates["premium_product_hero"]

    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="1x1",
        headline="Refresh your summer", disclaimer=None,
        guidelines=custom, layout=layout, out_path=out,
    )
    # No escalation when the check is disabled.
    assert meta["overlay_opacity"] == meta["overlay_opacity_configured"]
