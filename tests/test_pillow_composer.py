"""Phase 2 tests: smart_crop dimensions, compose_creative end-to-end, file_utils logic.

All inputs (aspect-ratio dimensions, layout knobs, typography sizing) flow from
the BrandGuidelines + LayoutTemplate fixtures, never from module constants.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines, LayoutTemplate
from creative_pipeline.tools.file_utils import output_path, pick_locale
from creative_pipeline.tools.pillow_composer import compose_creative, smart_crop


@pytest.fixture
def brand() -> BrandGuidelines:
    return BrandGuidelines.model_validate({
        "brand_id": "aquacorp_global",
        "brand_name": "AquaCorp",
        "voice_and_tone": {"personality": ["bright"], "avoid": ["clinical"]},
        "visual_identity": {
            "primary_color": "#00B4D8",
            "secondary_color": "#023E8A",
            "accent_color": "#90E0EF",
            "primary_colors": ["#00B4D8", "#FFFFFF", "#023E8A"],
            "accent_colors": ["#90E0EF"],
            "logo_path": "inputs/assets/global/logo.png",
            "logo_placement": "top-right",
            "safe_zone_pct": 0.08,
            "logo_height_pct": 0.10,
        },
        "typography": {
            "headline_font": "Montserrat-Bold.ttf",
            "body_font": "OpenSans-Regular.ttf",
            "fonts_dir": "fonts",
            "text_color_on_dark": "#FFFFFF",
            "text_color_on_light": "#023E8A",
            "headline_size_ratio": 0.07,
            "body_size_ratio": 0.022,
            "headline_case": "sentence",
        },
        "layout_templates": {
            "hero_full_bleed_footer": {
                "text_region_pct": 0.30,
                "scrim_opacity_pct": 0.40,
                "scrim_padding_pct": 0.04,
                "luminance_threshold": 140,
                "overlay_style": "scrim",
            },
            "premium_product_hero": {
                "overlay_style": "vertical_gradient",
                "overlay_opacity_pct": 0.65,
                "overlay_extent_pct": 0.50,
                "headline_size_ratio": 0.06,
                "body_size_ratio": 0.018,
                "accent_style": "side_rail",
                "accent_color_role": "primary",
                "disclaimer_placement": "bottom_corner",
                "text_align_default": "left",
                "per_aspect": {
                    "1x1":  {"headline_box": [0.06, 0.62, 0.78, 0.92], "text_align": "left"},
                    "9x16": {"headline_box": [0.06, 0.66, 0.94, 0.92], "text_align": "left"},
                    "16x9": {"headline_box": [0.04, 0.50, 0.55, 0.93], "text_align": "left"},
                },
            },
        },
        "aspect_ratios": {
            "1x1": [1080, 1080],
            "9x16": [1080, 1920],
            "16x9": [1920, 1080],
        },
        "imagery_style": {
            "mood": "bright outdoors",
            "avoid": ["dark"],
            "style_prompt_suffix": "photorealistic",
        },
        "creative_quality_presets": {
            "demo_polished": "polished",
        },
        "legal": {"prohibited_words": ["best"], "required_disclaimers": {"MX": "T&C"}},
    })


@pytest.fixture
def layout(brand) -> LayoutTemplate:
    return brand.layout_templates["hero_full_bleed_footer"]


@pytest.fixture
def fake_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (2000, 1500), (200, 220, 240))
    p = tmp_path / "hero.png"
    img.save(p)
    return str(p)


@pytest.mark.parametrize("ratio,expected", [("1x1", (1080, 1080)), ("9x16", (1080, 1920)), ("16x9", (1920, 1080))])
def test_smart_crop_produces_exact_target_dims(fake_hero, ratio, expected):
    src = Image.open(fake_hero)
    out = smart_crop(src, expected[0], expected[1])
    assert out.size == expected


def test_compose_creative_writes_all_three_ratios(brand, layout, fake_hero, tmp_path):
    out_dir = tmp_path / "outputs"
    paths = []
    for ratio, dims in brand.aspect_ratios.items():
        out_path = str(out_dir / "test_product" / ratio / "MX_es.png")
        meta = compose_creative(
            hero_path=fake_hero, ratio=ratio,
            headline="Refresh your summer", disclaimer="T&C apply",
            guidelines=brand, layout=layout, out_path=out_path,
        )
        assert meta["size"] == tuple(dims)
        assert meta["disclaimer_rendered"] is True
        with Image.open(out_path) as img:
            assert img.size == tuple(dims)
        paths.append(out_path)
    assert len(paths) == 3


def test_compose_creative_without_disclaimer(brand, layout, fake_hero, tmp_path):
    out_path = str(tmp_path / "no_disc.png")
    meta = compose_creative(
        hero_path=fake_hero, ratio="1x1",
        headline="Hello", disclaimer=None,
        guidelines=brand, layout=layout, out_path=out_path,
    )
    assert meta["disclaimer_rendered"] is False
    assert Path(out_path).exists()


def test_compose_creative_unknown_ratio_raises(brand, layout, fake_hero, tmp_path):
    with pytest.raises(ValueError, match="not defined"):
        compose_creative(
            hero_path=fake_hero, ratio="banner_3x1",
            headline="x", disclaimer=None,
            guidelines=brand, layout=layout,
            out_path=str(tmp_path / "x.png"),
        )


def test_pick_locale_uses_market_chain():
    chains = {"MX": ["es", "en"], "BR": ["pt", "en"], "CO": ["es", "en"], "US": ["en"]}
    assert pick_locale("MX", ["en", "es", "pt"], chains) == "es"
    assert pick_locale("BR", ["en", "es", "pt"], chains) == "pt"
    assert pick_locale("CO", ["en", "es", "pt"], chains) == "es"
    assert pick_locale("US", ["en", "es", "pt"], chains) == "en"
    # Unknown market falls back to language
    assert pick_locale("ZZ", ["en", "es"], chains) == "en"
    # Missing primary triggers fallback
    assert pick_locale("MX", ["en"], chains) == "en"


def test_pick_locale_raises_when_no_locale_available():
    chains = {"MX": ["es", "en"]}
    with pytest.raises(ValueError):
        pick_locale("MX", ["fr"], chains)


def test_output_path_format():
    p = output_path("outputs", "aquavita_sparkling", "1x1", "MX", "es")
    assert p == "outputs/aquavita_sparkling/1x1/MX_es.png"
