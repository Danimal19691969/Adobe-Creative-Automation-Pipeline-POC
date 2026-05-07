"""Per-aspect typography contract.

Confirms the brand-side YAML fields drive the composer's headline and
disclaimer sizing per aspect ratio:

  - inputs/brand/guidelines.yaml::typography.per_aspect.{1x1,9x16,16x9}
    define hard pixel min/max + target zone-fill + preferred line count
    for the headline, and pixel min/max + size-pct-of-height for the
    disclaimer.

  - The composer honors those bounds: 16:9 headlines are materially
    larger than 1:1 headlines, and 16:9 disclaimers are materially
    larger than 1:1 disclaimers, even though the global
    typography.headline_size_ratio / body_size_ratio are unchanged.

  - Min/max floors are hard: a deliberately tight headline_max forces
    the rendered size into that ceiling.
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
def neutral_hero(tmp_path: Path) -> str:
    """A mid-gray hero so contrast doesn't dominate the test outcome."""
    img = Image.new("RGB", (1024, 1024), (90, 90, 90))
    p = tmp_path / "hero.png"
    img.save(p)
    return str(p)


def test_per_aspect_typography_loads_from_yaml(brand_yaml):
    pa = brand_yaml.typography.per_aspect
    for ratio in ("1x1", "9x16", "16x9"):
        assert ratio in pa, f"missing per_aspect entry for {ratio}"
    # 16:9 should specify the bigger headline + disclaimer ceilings — that's
    # the whole point of the per-aspect override for that ratio.
    assert pa["16x9"].headline_max_font_size_px > pa["1x1"].headline_max_font_size_px
    assert pa["16x9"].disclaimer_max_font_size_px > pa["1x1"].disclaimer_max_font_size_px
    # All ranges internally consistent.
    for ratio, entry in pa.items():
        assert entry.headline_min_font_size_px <= entry.headline_max_font_size_px
        assert entry.disclaimer_min_font_size_px <= entry.disclaimer_max_font_size_px


def test_16x9_headline_larger_than_1x1(brand_yaml, neutral_hero, tmp_path):
    """The 16:9 render must produce a larger rendered headline than 1:1
    because the per-aspect ceiling is higher and the canvas accommodates it."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out_1x1 = str(tmp_path / "1x1.png")
    out_16x9 = str(tmp_path / "16x9.png")
    headline = "Refresh your summer, naturally."

    meta_1x1 = compose_creative(
        hero_path=neutral_hero, ratio="1x1", headline=headline, disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_1x1,
    )
    meta_16x9 = compose_creative(
        hero_path=neutral_hero, ratio="16x9", headline=headline, disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_16x9,
    )
    assert meta_16x9["headline_size_px"] > meta_1x1["headline_size_px"], (
        f"16x9 headline should be larger than 1x1; got "
        f"16x9={meta_16x9['headline_size_px']}, 1x1={meta_1x1['headline_size_px']}"
    )


def test_16x9_disclaimer_larger_than_1x1(brand_yaml, neutral_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out_1x1 = str(tmp_path / "1x1.png")
    out_16x9 = str(tmp_path / "16x9.png")

    meta_1x1 = compose_creative(
        hero_path=neutral_hero, ratio="1x1", headline="Hello", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_1x1,
    )
    meta_16x9 = compose_creative(
        hero_path=neutral_hero, ratio="16x9", headline="Hello", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_16x9,
    )
    assert meta_16x9["disclaimer_size_px"] > meta_1x1["disclaimer_size_px"], (
        f"16x9 disclaimer should be larger than 1x1; got "
        f"16x9={meta_16x9['disclaimer_size_px']}, 1x1={meta_1x1['disclaimer_size_px']}"
    )
    # Specific brand guarantee: 16:9 disclaimer must reach at least the
    # configured min (24 px in current YAML).
    assert meta_16x9["disclaimer_size_px"] >= brand_yaml.typography.per_aspect["16x9"].disclaimer_min_font_size_px


def test_headline_size_respects_brand_max_ceiling(brand_yaml, neutral_hero, tmp_path):
    """Tighten the brand's 16:9 headline_max and prove the composer respects it."""
    custom = brand_yaml.model_copy(deep=True)
    custom.typography.per_aspect["16x9"].headline_max_font_size_px = 40
    custom.typography.per_aspect["16x9"].headline_min_font_size_px = 30
    layout = custom.layout_templates["premium_product_hero"]

    out = str(tmp_path / "16x9.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="16x9", headline="Refresh your summer", disclaimer=None,
        guidelines=custom, layout=layout, out_path=out,
    )
    assert 30 <= meta["headline_size_px"] <= 40


def test_headline_size_respects_brand_min_floor(brand_yaml, neutral_hero, tmp_path):
    """Even with a tiny zone-fill that would 'want' very small text, the
    brand's headline_min_font_size_px is the hard floor."""
    custom = brand_yaml.model_copy(deep=True)
    custom.typography.per_aspect["16x9"].headline_min_font_size_px = 80
    custom.typography.per_aspect["16x9"].headline_max_font_size_px = 96
    custom.typography.per_aspect["16x9"].headline_target_zone_fill_pct = 0.05
    layout = custom.layout_templates["premium_product_hero"]

    out = str(tmp_path / "16x9.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="16x9",
        headline="Refresh your summer, naturally.", disclaimer=None,
        guidelines=custom, layout=layout, out_path=out,
    )
    # We accept exact floor (the function falls back to min when target_h is
    # unreachable), or any value within the configured band.
    assert meta["headline_size_px"] >= 80
    assert meta["headline_size_px"] <= 96


def test_meta_records_headline_and_disclaimer_size(brand_yaml, neutral_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "1x1.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="1x1", headline="Hi", disclaimer="T&C",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    assert isinstance(meta["headline_size_px"], int) and meta["headline_size_px"] > 0
    assert isinstance(meta["disclaimer_size_px"], int) and meta["disclaimer_size_px"] > 0
    assert meta["headline_line_count"] >= 1


def test_no_disclaimer_yields_none_size(brand_yaml, neutral_hero, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "1x1.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="1x1", headline="Hi", disclaimer=None,
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    assert meta["disclaimer_size_px"] is None


def test_per_aspect_ceilings_are_confident(brand_yaml):
    """Per the prominence-tuning pass, the brand commits to large-feeling
    headlines on clean photography. These bounds are the new floor for
    'campaign hero' rendering — going below them is a regression."""
    pa = brand_yaml.typography.per_aspect
    assert pa["1x1"].headline_max_font_size_px >= 78
    assert pa["1x1"].headline_target_zone_fill_pct >= 0.55
    assert pa["9x16"].headline_max_font_size_px >= 96
    assert pa["9x16"].headline_target_zone_fill_pct >= 0.55
    assert pa["16x9"].headline_max_font_size_px >= 110
    assert pa["16x9"].headline_target_zone_fill_pct >= 0.65


def test_compose_records_prominence_score(brand_yaml, neutral_hero, tmp_path):
    """compose_creative surfaces headline_prominence_score in [0, 1.5] and
    the configured min/max so reviewers can verify the composer used the
    ceiling instead of staying timid."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "1x1.png")
    meta = compose_creative(
        hero_path=neutral_hero, ratio="1x1",
        headline="Refresh your summer, naturally.", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    score = meta["headline_prominence_score"]
    assert isinstance(score, float)
    # On a clean neutral hero the composer should land well above the floor
    # (≥0.7 of the configured ceiling = "confident" hero copy).
    assert 0.0 < score <= 1.5
    assert score >= 0.7, (
        f"Headline rendered timidly on clean hero: prominence={score} "
        f"(size={meta['headline_size_px']}, max={meta['headline_max_size_px_configured']})"
    )
    assert meta["headline_max_size_px_configured"] == brand_yaml.typography.per_aspect["1x1"].headline_max_font_size_px
    assert meta["headline_min_size_px_configured"] == brand_yaml.typography.per_aspect["1x1"].headline_min_font_size_px


def test_16x9_headline_is_confident_on_clean_canvas(brand_yaml, tmp_path):
    """On a clean 16:9 canvas the composer must produce confident hero copy
    rather than caption-sized fallback text. We measure 'confident' as
    rendered headline_size_px floor — anything below 90px on the demo
    string would visibly read as a caption."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    out = str(tmp_path / "16x9.png")
    img = Image.new("RGB", (1920, 1080), (90, 110, 140))
    p = tmp_path / "clean16x9.png"
    img.save(p)
    meta = compose_creative(
        hero_path=str(p), ratio="16x9",
        headline="Refresh your summer, naturally.", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    assert meta["headline_size_px"] >= 90, (
        f"16x9 headline rendered as caption ({meta['headline_size_px']}px); "
        f"prominence={meta['headline_prominence_score']}"
    )
