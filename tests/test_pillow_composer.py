"""Phase 2 tests: smart_crop dimensions, compose_creative end-to-end, file_utils logic."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines
from creative_pipeline.tools.file_utils import ASPECT_RATIOS, output_path, pick_locale
from creative_pipeline.tools.pillow_composer import compose_creative, smart_crop


@pytest.fixture
def brand() -> BrandGuidelines:
    return BrandGuidelines.model_validate({
        "brand_id": "aquacorp_global",
        "voice_and_tone": {"personality": ["bright"], "avoid": ["clinical"]},
        "visual_identity": {
            "primary_colors": ["#00B4D8", "#FFFFFF", "#023E8A"],
            "accent_colors": ["#90E0EF"],
            "logo_path": "inputs/assets/global/logo.png",
            "logo_placement": "top-right",
            "safe_zone_pct": 0.08,
        },
        "typography": {
            "headline_font": "Montserrat-Bold.ttf",
            "body_font": "OpenSans-Regular.ttf",
            "text_color_on_dark": "#FFFFFF",
            "text_color_on_light": "#023E8A",
        },
        "imagery_style": {
            "mood": "bright outdoors",
            "avoid": ["dark"],
            "style_prompt_suffix": "photorealistic",
        },
        "legal": {"prohibited_words": ["best"], "required_disclaimers": {"MX": "T&C"}},
    })


@pytest.fixture
def fake_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (2000, 1500), (200, 220, 240))
    p = tmp_path / "hero.png"
    img.save(p)
    return str(p)


@pytest.mark.parametrize("ratio,expected", list(ASPECT_RATIOS.items()))
def test_smart_crop_produces_exact_target_dims(fake_hero, ratio, expected):
    src = Image.open(fake_hero)
    out = smart_crop(src, expected[0], expected[1])
    assert out.size == expected


def test_compose_creative_writes_all_three_ratios(brand, fake_hero, tmp_path):
    out_dir = tmp_path / "outputs"
    paths = []
    for ratio, dims in ASPECT_RATIOS.items():
        out_path = str(out_dir / "test_product" / ratio / "MX_es.png")
        meta = compose_creative(
            hero_path=fake_hero,
            ratio=ratio,
            headline="Refresh your summer",
            disclaimer="T&C apply",
            guidelines=brand,
            out_path=out_path,
        )
        assert meta["size"] == dims
        assert meta["disclaimer_rendered"] is True
        assert Path(out_path).exists()
        # Verify output PNG matches target canvas size exactly
        with Image.open(out_path) as img:
            assert img.size == dims
        paths.append(out_path)
    assert len(paths) == 3


def test_compose_creative_without_disclaimer(brand, fake_hero, tmp_path):
    out_path = str(tmp_path / "no_disc.png")
    meta = compose_creative(
        hero_path=fake_hero,
        ratio="1x1",
        headline="Hello",
        disclaimer=None,
        guidelines=brand,
        out_path=out_path,
    )
    assert meta["disclaimer_rendered"] is False
    assert Path(out_path).exists()


def test_pick_locale_uses_market_chain():
    assert pick_locale("MX", ["en", "es", "pt"]) == "es"
    assert pick_locale("BR", ["en", "es", "pt"]) == "pt"
    assert pick_locale("CO", ["en", "es", "pt"]) == "es"
    assert pick_locale("US", ["en", "es", "pt"]) == "en"
    # Unknown market falls back to en
    assert pick_locale("ZZ", ["en", "es"]) == "en"
    # Missing primary triggers fallback
    assert pick_locale("MX", ["en"]) == "en"


def test_pick_locale_raises_when_no_locale_available():
    with pytest.raises(ValueError):
        pick_locale("MX", ["fr"])


def test_output_path_format():
    p = output_path("outputs", "aquavita_sparkling", "1x1", "MX", "es")
    assert p == "outputs/aquavita_sparkling/1x1/MX_es.png"
