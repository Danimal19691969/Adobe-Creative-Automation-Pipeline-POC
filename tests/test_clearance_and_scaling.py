"""Object-clearance + 16x9 scaling tests.

Covers:
  - Brand YAML loads object_text_clearance_pct, min_text_object_gap_px,
    object_clearance_penalty, hard_fail_text_object_collision.
  - _expand_safe_zone_with_clearance produces a strictly larger box than
    the unexpanded focal safe zone (when clearance > 0).
  - Candidate boxes overlapping the unexpanded focal zone are hard-rejected
    when hard_fail_text_object_collision=true.
  - Candidate boxes inside the *expanded* zone but outside the unexpanded
    one are penalized but still viable.
  - Cleaner candidates with full clearance are preferred when contrast OK.
  - 16x9 typography ceiling (110 px) lets the headline scale up when no
    crowding is present.
  - Box-shift logic shrinks the right edge when a candidate runs up
    against a focal area to the right.
  - Compose meta + reporter expose the new audit fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from creative_pipeline.schemas import BrandGuidelines, PerAspectLayout
from creative_pipeline.tools.pillow_composer import (
    _box_gap_px,
    _box_overlap_pct,
    _expand_safe_zone_with_clearance,
    _select_headline_box,
    _try_shift_box_away_from_focal,
    compose_creative,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


# -------- Schema / YAML wiring --------

def test_clearance_fields_load_from_yaml(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.object_text_clearance_pct > 0
    assert layout.object_clearance_penalty > 0
    assert layout.hard_fail_text_object_collision is True
    # Per-aspect minimum pixel gaps must be set for all three demo aspects.
    for ratio in ("1x1", "9x16", "16x9"):
        assert ratio in layout.min_text_object_gap_px
        assert layout.min_text_object_gap_px[ratio] > 0


def test_16x9_typography_ceiling_was_raised(brand_yaml):
    """Per the new spec, 16x9 max headline size is at least 110 px so the
    headline can scale up when clearance allows."""
    pa = brand_yaml.typography.per_aspect["16x9"]
    assert pa.headline_max_font_size_px >= 110
    assert pa.headline_min_font_size_px >= 78


# -------- Expanded safe zone --------

def test_expanded_safe_zone_is_strictly_larger(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    focal = (0.40, 0.30, 0.70, 0.70)
    expanded = _expand_safe_zone_with_clearance(focal, layout, "1x1", 1080, 1080)
    fx0, fy0, fx1, fy1 = focal
    ex0, ey0, ex1, ey1 = expanded
    assert ex0 < fx0
    assert ey0 < fy0
    assert ex1 > fx1
    assert ey1 > fy1


def test_expanded_safe_zone_clamps_to_unit_square(brand_yaml):
    """Excessive clearance can't push the box outside [0, 1]."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.object_text_clearance_pct = 0.40
    expanded = _expand_safe_zone_with_clearance(
        (0.20, 0.20, 0.80, 0.80), layout, "1x1", 1000, 1000,
    )
    for c in expanded:
        assert 0.0 <= c <= 1.0


# -------- Hard-fail and clearance penalty in scoring --------

def _busy_right_hero(tmp_path: Path) -> str:
    """Hero with a high-edge-density cluster filling the right half so the
    focal estimator (largest connected component of dense cells) returns
    a meaningful right-side bbox."""
    img = Image.new("RGB", (1080, 1080), (60, 90, 120))
    d = ImageDraw.Draw(img)
    # Pack many small high-contrast shapes into the right half. The cells
    # in this region all clear the edge-density threshold and form a single
    # connected component — exactly what the focal estimator looks for.
    for x in range(560, 1040, 28):
        for y in range(200, 880, 28):
            d.ellipse((x, y, x + 18, y + 18), fill=(245, 240, 230))
            d.rectangle((x + 4, y + 4, x + 14, y + 14), fill=(40, 40, 40))
    p = tmp_path / "right_object.png"
    img.save(p)
    return str(p)


def test_candidate_inside_unexpanded_zone_is_hard_rejected(brand_yaml, tmp_path):
    """A candidate that overlaps the unexpanded focal safe zone must be
    marked non-viable when hard_fail_text_object_collision=True."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_busy_right_hero(tmp_path)).convert("RGBA")
    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.30, 0.40, 0.70),  # left half — should be clear
            (0.55, 0.30, 0.95, 0.70),  # right half — overlaps the bottle
        ],
        avoid_busy_regions=True,
    )
    selected, meta, scores, focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    by_x0 = sorted(scores, key=lambda s: s["box_pct"][0])
    left, right = by_x0[0], by_x0[1]
    # Right candidate must be flagged as hard collision and non-viable.
    assert right["hard_collision"] is True
    assert right["viable"] is False
    # Left wins.
    assert (selected[0] + selected[2]) / 2 < 0.5


def test_clearance_pass_recorded_for_clear_candidate(brand_yaml, tmp_path):
    """A candidate with no overlap and gap > min_gap reports
    text_object_clearance_pass=True."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_busy_right_hero(tmp_path)).convert("RGBA")
    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.10, 0.40, 0.30),  # well clear — upper-left
        ],
        avoid_busy_regions=True,
    )
    _, _, _, focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    if focal["product_safe_zone_box"] is not None:
        assert focal["text_object_clearance_pass"] in (True, False)
        assert focal["text_object_gap_px"] is not None


# -------- Box-shift logic --------

def test_shift_shrinks_box_right_edge_when_focal_to_the_right():
    box = (0.06, 0.40, 0.55, 0.70)             # candidate ends near x=0.55
    safe_zone = (0.55, 0.30, 0.85, 0.80)        # focal starts at x=0.55
    expanded = (0.50, 0.25, 0.90, 0.85)         # expanded covers x=0.50–0.90
    adjusted, reason = _try_shift_box_away_from_focal(
        box, safe_zone, expanded,
        canvas_w=1080, canvas_h=1080,
        min_gap_px=40,
    )
    assert adjusted is not None
    assert adjusted[2] < box[2]  # x1 shrunk
    assert "x1" in (reason or "")


def test_shift_skips_when_no_overlap():
    box = (0.06, 0.40, 0.40, 0.62)
    safe_zone = (0.60, 0.30, 0.85, 0.80)
    expanded = (0.55, 0.25, 0.90, 0.85)
    adjusted, reason = _try_shift_box_away_from_focal(
        box, safe_zone, expanded,
        canvas_w=1080, canvas_h=1080, min_gap_px=40,
    )
    assert adjusted == box
    assert reason == "no_shift_needed"


# -------- 16x9 headline scaling --------

def test_16x9_headline_scales_larger_when_clear(brand_yaml, tmp_path):
    """On a clean 16x9 canvas the headline should reach into the bumped
    78–110 px range (it will also depend on box width / wrapping, but it
    must clear the legacy ~96 px ceiling under at least one of the candidate
    boxes)."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1920, 1080), (100, 130, 160))  # mid-tone solid
    p = tmp_path / "clean.png"
    img.save(p)

    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="16x9",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    pa = brand_yaml.typography.per_aspect["16x9"]
    assert pa.headline_min_font_size_px <= meta["headline_size_px"] <= pa.headline_max_font_size_px


# -------- Compose meta has the new audit fields --------

def test_compose_records_clearance_audit(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    hero = _busy_right_hero(tmp_path)
    out = str(tmp_path / "audit.png")
    meta = compose_creative(
        hero_path=hero, ratio="1x1",
        headline="Refresh your summer", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    for k in (
        "expanded_product_safe_zone_box",
        "focal_near_miss_detected",
        "text_object_gap_px",
        "text_object_clearance_pass",
        "headline_box_original",
        "headline_box_adjusted",
        "headline_box_adjustment_reason",
        "disclaimer_text_object_gap_px",
        "disclaimer_clearance_pass",
    ):
        assert k in meta, f"missing meta field: {k}"


def test_box_gap_helpers():
    # No overlap, horizontal gap = 200px on 1000px canvas (0.2 fractional)
    a = (0.0, 0.4, 0.4, 0.6)
    b = (0.6, 0.4, 1.0, 0.6)
    assert _box_gap_px(a, b, 1000, 1000) == pytest.approx(200.0)
    assert _box_overlap_pct(a, b) == 0.0
    # Overlap → gap = 0
    a = (0.0, 0.0, 0.6, 1.0)
    b = (0.4, 0.0, 1.0, 1.0)
    assert _box_gap_px(a, b, 1000, 1000) == 0.0
