"""Focal-area estimation + line-count + hard-cap tests.

Locks in the deterministic focal-area heuristic, the line-count constraints
in the headline fitter, and the hard rejection caps on candidate boxes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw, ImageFont

from creative_pipeline.schemas import BrandGuidelines, PerAspectLayout
from creative_pipeline.tools.pillow_composer import (
    _box_overlap_pct,
    _estimate_focal_area,
    _expand_box_pct,
    _fit_text_with_pixel_bounds,
    _select_headline_box,
    compose_creative,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


# -------- Focal-area estimator --------

def test_focal_area_finds_dense_region(tmp_path: Path):
    """A canvas with a dense object cluster on the right half should produce
    a focal bbox biased to the right."""
    img = Image.new("RGB", (1080, 1080), (240, 240, 240))
    d = ImageDraw.Draw(img)
    # A bunch of small high-contrast shapes packed on the right half.
    for x in range(560, 1040, 30):
        for y in range(400, 800, 30):
            d.ellipse((x, y, x + 20, y + 20), fill=(40, 60, 100))
    p = tmp_path / "right_dense.png"
    img.save(p)

    bbox, density = _estimate_focal_area(Image.open(p))
    assert bbox is not None
    x0, y0, x1, y1 = bbox
    # Box's center-x should sit on the right half.
    assert (x0 + x1) / 2 > 0.5
    assert density > 0.0


def test_focal_area_returns_none_for_flat_photo():
    """A perfectly flat image has no focal cluster — estimator returns None."""
    img = Image.new("RGB", (640, 640), (120, 120, 120))
    bbox, _ = _estimate_focal_area(img)
    assert bbox is None


def test_expand_box_pct_clamps_to_unit_square():
    assert _expand_box_pct((0.0, 0.0, 0.5, 0.5), 0.10) == (0.0, 0.0, 0.6, 0.6)
    # large pad clamps to 1.0
    assert _expand_box_pct((0.4, 0.4, 0.6, 0.6), 0.5) == (0.0, 0.0, 1.0, 1.0)


def test_box_overlap_pct():
    assert _box_overlap_pct((0, 0, 0.5, 0.5), (0.25, 0.25, 0.75, 0.75)) == pytest.approx(0.25)
    assert _box_overlap_pct((0.0, 0.0, 0.4, 0.4), (0.6, 0.6, 1.0, 1.0)) == 0.0


# -------- Hard caps + focal-overlap penalty in candidate scoring --------

def _make_split_with_focal_right(tmp_path: Path) -> str:
    """Hero with clean dark left + busy striped + focal cluster on the right."""
    img = Image.new("RGB", (1080, 1080), (10, 30, 60))  # dark navy
    d = ImageDraw.Draw(img)
    # Right half: high-frequency stripes (high edge density)
    for x in range(540, 1080, 8):
        d.rectangle((x, 0, x + 4, 1080), fill=(255, 240, 180))
    p = tmp_path / "right_dense_split.png"
    img.save(p)
    return str(p)


def test_focal_overlap_penalizes_right_candidate(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_split_with_focal_right(tmp_path)).convert("RGBA")

    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.30, 0.45, 0.70),   # left half — clean
            (0.55, 0.30, 0.95, 0.70),   # right half — overlaps focal cluster
        ],
        avoid_busy_regions=True,
    )

    selected, meta, scores, focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    # Focal area must be detected.
    assert focal["focal_area_estimate"] is not None
    assert focal["product_safe_zone_box"] is not None
    # Selected box must be the LEFT candidate.
    assert (selected[0] + selected[2]) / 2 < 0.5
    # The right candidate must report a higher focal overlap fraction.
    by_x0 = sorted(scores, key=lambda s: s["box_pct"][0])
    left, right = by_x0[0], by_x0[1]
    assert right["focal_overlap_pct"] >= left["focal_overlap_pct"]


def test_hard_caps_mark_extreme_candidate_non_viable(brand_yaml, tmp_path):
    """Set a tight max_edge_density cap so the right (striped) candidate is
    rejected as non-viable; scorer must pick the left even if its raw score
    would otherwise be lower."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    # Tight caps so the busy stripes are over the bar. Disable near-miss
    # hard-fail for this isolation test — we're verifying edge-density
    # rejection independently of clearance rules.
    layout.text_region_max_edge_density = 0.04
    layout.hard_fail_text_object_near_miss = False

    cropped = Image.open(_make_split_with_focal_right(tmp_path)).convert("RGBA")
    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.55, 0.30, 0.95, 0.70),   # busy stripes — should be non-viable
            (0.05, 0.30, 0.45, 0.70),   # clean
        ],
        avoid_busy_regions=True,
    )

    selected, meta, scores, focal = _select_headline_box(
        cropped, layout, pa, custom, target_w=1080, target_h=1080,
    )
    # The clean candidate must win.
    assert (selected[0] + selected[2]) / 2 < 0.5
    by_x0 = sorted(scores, key=lambda s: s["box_pct"][0])
    left, right = by_x0[0], by_x0[1]
    assert left["viable"] is True
    assert right["viable"] is False
    assert any("edge_norm" in r for r in right["rejection_reasons"])


# -------- Line-count constraints in the fitter --------

def test_fitter_respects_max_line_count(tmp_path):
    """A long headline that would naturally wrap to 4 lines at the largest
    fitting size must be capped to max_line_count by the fitter."""
    canvas = Image.new("RGB", (1080, 1080))
    draw = ImageDraw.Draw(canvas)
    long_headline = "Refresh your summer with this remarkable natural sparkling water today"

    font, lines, total_h, scale_reason, wrap_strategy = _fit_text_with_pixel_bounds(
        draw, long_headline,
        font_filename="Montserrat-Bold.ttf",
        fonts_dir="fonts",
        min_size_px=24,
        max_size_px=80,
        box_w=600,
        target_h=400,
        preferred_line_count=2,
        min_line_count=2,
        max_line_count=3,
    )
    assert len(lines) <= 3
    assert "lines" in scale_reason or "fitting" in scale_reason
    assert wrap_strategy in ("greedy", "balanced")


def test_fitter_returns_scale_reason_string(tmp_path):
    canvas = Image.new("RGB", (400, 400))
    draw = ImageDraw.Draw(canvas)
    font, lines, total_h, scale_reason, wrap_strategy = _fit_text_with_pixel_bounds(
        draw, "Hi there",
        font_filename="Montserrat-Bold.ttf",
        fonts_dir="fonts",
        min_size_px=20,
        max_size_px=80,
        box_w=300,
        target_h=200,
    )
    assert isinstance(scale_reason, str)
    assert scale_reason  # non-empty
    assert wrap_strategy in ("greedy", "balanced")


# -------- End-to-end meta fields --------

def test_compose_records_focal_and_wrap_audit(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    hero_path = _make_split_with_focal_right(tmp_path)

    out_path = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=hero_path, ratio="1x1",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_path,
    )
    for k in (
        "headline_color_selected",
        "headline_wrap_variant",
        "headline_scale_reason",
        "headline_fit_status",
        "focal_area_estimate",
        "product_safe_zone_box",
        "focal_overlap_detected",
        "focal_overlap_pct",
    ):
        assert k in meta, f"missing meta field: {k}"
    assert meta["headline_color_selected"] in {
        brand_yaml.typography.text_color_on_dark,
        brand_yaml.typography.text_color_on_light,
    }
    assert meta["headline_fit_status"] in {"fits", "near_fit", "overflow"}
    assert "_lines_at_" in meta["headline_wrap_variant"]


def test_compose_falls_back_when_scoring_disabled(brand_yaml, tmp_path):
    """enable_candidate_headline_scoring=false → composer uses the single
    headline_box and reports the fallback reason. Existing test coverage
    for the fallback path is preserved."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.enable_candidate_headline_scoring = False

    img = Image.new("RGB", (1024, 1024), (90, 110, 130))
    p = tmp_path / "neutral.png"
    img.save(p)

    out = str(tmp_path / "fb.png")
    meta = compose_creative(
        hero_path=str(p), ratio="1x1",
        headline="Hi", disclaimer=None,
        guidelines=custom, layout=layout, out_path=out,
    )
    assert "single_configured_box" in meta["headline_box_selection_reason"]
    assert meta["headline_box_candidates"] == []
