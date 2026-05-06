"""Tests for palette extraction and logo detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from creative_pipeline.tools.color_analyzer import (
    detect_logo,
    dominant_palette,
    palette_distance,
)


@pytest.fixture
def solid_blue_image(tmp_path) -> str:
    img = Image.new("RGB", (256, 256), (0, 180, 216))  # #00B4D8
    p = tmp_path / "blue.png"
    img.save(p)
    return str(p)


def test_dominant_palette_finds_solid_color(solid_blue_image):
    palette = dominant_palette(solid_blue_image, n=3)
    assert palette[0] == "#00B4D8" or palette[0].startswith("#00B")


def test_palette_distance_zero_for_exact_match():
    d = palette_distance(["#00B4D8"], ["#00B4D8"])
    assert d == 0.0


def test_palette_distance_increases_with_difference():
    near = palette_distance(["#00B4D8"], ["#00B0D0"])
    far = palette_distance(["#00B4D8"], ["#FF0000"])
    assert near < far


def test_palette_distance_handles_empty():
    assert palette_distance([], ["#00B4D8"]) == float("inf")
    assert palette_distance(["#00B4D8"], []) == float("inf")


def test_detect_logo_finds_stamped_logo(tmp_path):
    # Build a 1080x1080 canvas with a small blue circle "logo" stamped in top-right
    logo = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ld = ImageDraw.Draw(logo)
    ld.ellipse((10, 10, 190, 190), fill=(0, 180, 216, 255))
    logo_path = str(tmp_path / "logo.png")
    logo.save(logo_path)

    canvas = Image.new("RGB", (1080, 1080), (240, 240, 240))
    # Stamp scaled-down logo (matches composer's 10% of min dim = 108px) into top-right
    target = 108
    logo_scaled = logo.resize((target, target), Image.LANCZOS)
    canvas.paste(logo_scaled, (1080 - target - 86, 86), logo_scaled)
    canvas_path = str(tmp_path / "canvas.png")
    canvas.save(canvas_path)

    result = detect_logo(canvas_path, logo_path, "top-right")
    assert result["found"] is True
    assert result["placement_ok"] is True
    assert result["match_score"] > 0.7


def test_detect_logo_flags_wrong_quadrant(tmp_path):
    logo = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    ld = ImageDraw.Draw(logo)
    ld.ellipse((10, 10, 190, 190), fill=(50, 100, 200, 255))
    logo_path = str(tmp_path / "logo.png")
    logo.save(logo_path)

    canvas = Image.new("RGB", (1080, 1080), (240, 240, 240))
    target = 108
    logo_scaled = logo.resize((target, target), Image.LANCZOS)
    canvas.paste(logo_scaled, (86, 86), logo_scaled)  # top-LEFT not top-right
    canvas_path = str(tmp_path / "canvas.png")
    canvas.save(canvas_path)

    result = detect_logo(canvas_path, logo_path, "top-right")
    assert result["found"] is True
    assert result["placement_ok"] is False  # logo is in top-left, not top-right
