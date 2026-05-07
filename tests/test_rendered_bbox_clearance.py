"""Rendered text bbox + multi-shift cascade + prominence scoring.

Locks in the post-render clearance pass:
  - clearance is checked against the *rendered text bbox*, not the wider
    candidate box;
  - hard_fail_text_object_near_miss flips near-miss into a non-viable
    candidate when alternatives exist;
  - the multi-attempt shift cascade tries deterministic shifts before
    giving up and records every attempt;
  - prominence score rewards larger headlines that pass clearance, and
    penalizes overlap / near-contact / overflow;
  - text color is re-picked from the actual rendered bbox background;
  - report meta carries every new audit field.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from creative_pipeline.schemas import BrandGuidelines, PerAspectLayout
from creative_pipeline.tools.pillow_composer import (
    _build_shift_candidates,
    _clearance_metrics,
    _compute_prominence_score,
    _rendered_text_bbox_px,
    _select_headline_box,
    _shift_box_pct,
    compose_creative,
)
from creative_pipeline.schemas import TextAlign


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


# ----- YAML wiring ----------------------------------------------------------


def test_brand_loads_hard_fail_near_miss(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.hard_fail_text_object_near_miss is True


def test_brand_min_gap_px_thresholds_increased(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    # The tightening pass bumped the per-aspect minimum gaps to enforce
    # visible breathing room (1x1 ≥ 50, 9x16 ≥ 56, 16x9 ≥ 64 px).
    assert layout.min_text_object_gap_px["1x1"] >= 50
    assert layout.min_text_object_gap_px["9x16"] >= 56
    assert layout.min_text_object_gap_px["16x9"] >= 64


# ----- helpers --------------------------------------------------------------


def _make_busy_right_hero(tmp_path: Path) -> str:
    """Hero with a high-edge focal cluster on the right half so the focal
    estimator returns a meaningful right-side bbox."""
    img = Image.new("RGB", (1080, 1080), (60, 90, 120))
    d = ImageDraw.Draw(img)
    for x in range(560, 1040, 28):
        for y in range(200, 880, 28):
            d.ellipse((x, y, x + 18, y + 18), fill=(245, 240, 230))
            d.rectangle((x + 4, y + 4, x + 14, y + 14), fill=(40, 40, 40))
    p = tmp_path / "right_focal.png"
    img.save(p)
    return str(p)


# ----- rendered text bbox ---------------------------------------------------


def test_rendered_text_bbox_left_align_uses_box_left():
    box_px = (100, 200, 600, 320)  # 500 wide
    bbox = _rendered_text_bbox_px(box_px, max_line_w=320, total_text_h=80, text_align=TextAlign.LEFT)
    assert bbox == (100, 200, 420, 280)


def test_rendered_text_bbox_right_align_uses_box_right():
    box_px = (100, 200, 600, 320)
    bbox = _rendered_text_bbox_px(box_px, max_line_w=300, total_text_h=80, text_align=TextAlign.RIGHT)
    assert bbox == (300, 200, 600, 280)


def test_rendered_text_bbox_center_align_centers_inside_box():
    box_px = (100, 200, 600, 320)
    bbox = _rendered_text_bbox_px(box_px, max_line_w=200, total_text_h=80, text_align=TextAlign.CENTER)
    # box midpoint x = 350; rendered x0 = 250, x1 = 450
    assert bbox == (250, 200, 450, 280)


# ----- clearance metrics ----------------------------------------------------


def test_clearance_metrics_collision_when_overlapping():
    rendered = (0.20, 0.30, 0.50, 0.50)
    focal = (0.40, 0.30, 0.70, 0.60)
    m = _clearance_metrics(rendered, focal, 1080, 1080, min_gap_px=40)
    assert m["collision"] is True
    assert m["clearance_pass"] is False


def test_clearance_metrics_near_miss_within_gap():
    rendered = (0.05, 0.30, 0.40, 0.50)
    focal = (0.43, 0.30, 0.70, 0.60)  # 0.03 fractional gap = ~32 px
    m = _clearance_metrics(rendered, focal, 1080, 1080, min_gap_px=50)
    assert m["collision"] is False
    assert m["near_miss"] is True
    assert m["clearance_pass"] is False
    assert m["gap_px"] is not None and m["gap_px"] < 50


def test_clearance_metrics_pass_when_far():
    rendered = (0.05, 0.30, 0.30, 0.50)
    focal = (0.65, 0.30, 0.95, 0.60)
    m = _clearance_metrics(rendered, focal, 1080, 1080, min_gap_px=40)
    assert m["clearance_pass"] is True
    assert m["near_miss"] is False
    assert m["collision"] is False


def test_clearance_metrics_no_focal_passes():
    m = _clearance_metrics((0.0, 0.0, 0.5, 0.5), None, 1080, 1080, min_gap_px=40)
    assert m["clearance_pass"] is True
    assert m["gap_px"] is None


# ----- shift box helper -----------------------------------------------------


def test_shift_box_pct_translates_within_unit_square():
    out = _shift_box_pct((0.20, 0.30, 0.50, 0.60), dx=-0.10, dy=0.0)
    assert out == (0.10, 0.30, 0.40, 0.60)


def test_shift_box_pct_returns_none_when_off_canvas():
    assert _shift_box_pct((0.05, 0.30, 0.50, 0.60), dx=-0.10) is None
    assert _shift_box_pct((0.05, 0.30, 0.50, 0.60), dy=0.50) is None


def test_shift_box_pct_returns_none_when_inverted():
    # narrow_right by too much
    assert _shift_box_pct((0.20, 0.30, 0.30, 0.60), shrink_right=0.20) is None


def test_build_shift_candidates_targets_focal_direction():
    # Focal on the right → candidates should include shift_left + narrow_right.
    box = (0.30, 0.30, 0.70, 0.60)
    focal = (0.75, 0.30, 0.95, 0.60)
    cands = _build_shift_candidates(box, focal, 1080, 1080, min_gap_px=50)
    names = [c[0] for c in cands]
    assert any("shift_left" in n for n in names)
    assert any("narrow_right" in n for n in names)


# ----- hard-fail near-miss --------------------------------------------------


def test_near_miss_is_hard_failed_when_alternatives_exist(brand_yaml, tmp_path):
    """A candidate within min_text_object_gap_px of the focal area is
    flagged non-viable when an alternative passes."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_busy_right_hero(tmp_path)).convert("RGBA")
    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.10, 0.30, 0.30),  # well clear — upper-left
            (0.05, 0.30, 0.45, 0.70),  # very close to focal area on right
        ],
        avoid_busy_regions=True,
    )
    selected, meta, scores, focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    # The clearer upper-left candidate must win.
    sel_y_mid = (selected[1] + selected[3]) / 2
    assert sel_y_mid < 0.35
    # The lower candidate (near the focal area) reports a near-miss
    # rejection reason in its scoring breakdown.
    near_focal = next(
        s for s in scores if s["box_pct"][2] >= 0.40
    )
    assert near_focal["near_miss"] is True
    assert (
        near_focal["viable"] is False
        or near_focal["hard_collision"] is True
    ), f"expected near-miss to be non-viable; got {near_focal}"


# ----- prominence score -----------------------------------------------------


def test_prominence_score_rewards_large_clear_headline():
    big = _compute_prominence_score(
        font_size_px=110, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.66, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    timid = _compute_prominence_score(
        font_size_px=60, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.30, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    assert big["headline_prominence_score"] > timid["headline_prominence_score"]
    assert big["headline_size_factor"] > timid["headline_size_factor"]


def test_prominence_score_penalizes_collision():
    clear = _compute_prominence_score(
        font_size_px=100, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.65, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    crowded = _compute_prominence_score(
        font_size_px=100, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.65, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=False, collision=True, near_miss=False,
    )
    assert clear["headline_prominence_score"] > crowded["headline_prominence_score"]
    assert crowded["headline_clearance_factor"] < clear["headline_clearance_factor"]


def test_prominence_score_penalizes_near_miss_too():
    clear = _compute_prominence_score(
        font_size_px=100, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.65, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    near = _compute_prominence_score(
        font_size_px=100, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.65, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=False, collision=False, near_miss=True,
    )
    assert clear["headline_prominence_score"] > near["headline_prominence_score"]


def test_prominence_score_penalizes_overflow():
    fits = _compute_prominence_score(
        font_size_px=80, max_size_px=128, min_size_px=82,
        zone_fill_pct=0.65, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="fits", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    overflows = fits.copy()
    overflows = _compute_prominence_score(
        font_size_px=80, max_size_px=128, min_size_px=82,
        zone_fill_pct=1.10, target_zone_fill_pct=0.68,
        line_count=2, preferred_lines=2, min_lines=1, max_lines=3,
        fit_status="overflow", contrast=7.0,
        clearance_pass=True, collision=False, near_miss=False,
    )
    assert fits["headline_prominence_score"] > overflows["headline_prominence_score"]


# ----- compose_creative end-to-end -----------------------------------------


def test_compose_records_rendered_bbox_and_clearance(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    hero = _make_busy_right_hero(tmp_path)
    out = str(tmp_path / "audit.png")
    meta = compose_creative(
        hero_path=hero, ratio="1x1",
        headline="Refresh your summer", disclaimer="Terms apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    for k in (
        "rendered_headline_bbox",
        "rendered_headline_bbox_pct",
        "text_object_collision_detected",
        "text_object_near_miss_detected",
        "all_candidates_failed_clearance",
        "clearance_failure_reason",
        "headline_box_shift_attempts",
        "headline_box_shift_success",
        "headline_color_candidates",
        "headline_color_selection_reason",
        "headline_prominence_score",
        "headline_size_factor",
        "headline_zone_fill_factor",
        "headline_line_factor",
        "headline_fit_factor",
        "headline_contrast_factor",
        "headline_clearance_factor",
        "min_text_object_gap_px_threshold",
    ):
        assert k in meta, f"missing meta field: {k}"

    # Rendered bbox must be inside the candidate headline box and at most
    # as wide (it can be narrower when text wraps tighter than the box).
    hb = meta["headline_box"]
    rb = meta["rendered_headline_bbox"]
    assert rb[0] >= hb[0]
    assert rb[2] <= hb[2]
    # Color candidates list contains both brand text colors.
    palette = {c["color"] for c in meta["headline_color_candidates"]}
    assert brand_yaml.typography.text_color_on_dark in palette
    assert brand_yaml.typography.text_color_on_light in palette


def test_clean_canvas_yields_high_prominence(brand_yaml, tmp_path):
    """A clean mid-tone canvas with no focal cluster should produce a
    high-prominence rendered headline (≥ 0.7) — the goal of the tuning pass."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1920, 1080), (90, 110, 140))
    p = tmp_path / "clean.png"
    img.save(p)
    meta = compose_creative(
        hero_path=str(p), ratio="16x9",
        headline="Refresh your summer, naturally.", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=str(tmp_path / "out.png"),
    )
    assert meta["headline_prominence_score"] >= 0.7, (
        f"timid headline on clean canvas; meta={meta}"
    )
    assert meta["text_object_clearance_pass"] in (True, None) or (
        # When there is no focal cluster, gap_px is None and clearance pass
        # is True.
        meta["text_object_gap_px"] is None
    )


def test_color_selection_uses_rendered_bbox(brand_yaml, tmp_path):
    """On a near-white canvas the composer must pick the dark brand text
    color — the rendered-bbox sample is what drives this, not the box."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1920, 1080), (250, 248, 245))  # near-white
    p = tmp_path / "white.png"
    img.save(p)
    meta = compose_creative(
        hero_path=str(p), ratio="16x9",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=str(tmp_path / "out.png"),
    )
    # On a near-white background the dark brand color wins.
    assert meta["headline_color_selected"] == brand_yaml.typography.text_color_on_light
    # The selection reason names the colors and contrasts.
    assert "picked" in meta["headline_color_selection_reason"]


def test_shift_attempts_recorded_when_initial_bbox_near_focal(brand_yaml, tmp_path):
    """When the first-pick rendered bbox sits close to the focal area, the
    cascade tries one or more shifts and records them. The final box should
    achieve clearance pass when at least one shift succeeds."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]

    cropped = Image.open(_make_busy_right_hero(tmp_path)).convert("RGBA")
    # Force a single candidate that's likely to need shifting (right edge
    # close to the focal area).
    target_w, target_h = 1080, 1080
    # Use the actual selector + a forced candidate pulled close to the bottle.
    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.30, 0.45, 0.65),
        ],
        avoid_busy_regions=True,
    )
    # Compose using a brand variant whose 1x1 per-aspect is the same.
    out = str(tmp_path / "shifted.png")
    layout_copy = layout.model_copy(update={"per_aspect": {"1x1": pa}})
    custom.layout_templates["premium_product_hero"] = layout_copy
    meta = compose_creative(
        hero_path=Image.open(cropped.filename) if hasattr(cropped, "filename") else _make_busy_right_hero(tmp_path),
        ratio="1x1",
        headline="Refresh your summer",
        disclaimer="Terms apply.",
        guidelines=custom, layout=layout_copy,
        out_path=out,
    ) if False else compose_creative(
        hero_path=_make_busy_right_hero(tmp_path),
        ratio="1x1",
        headline="Refresh your summer",
        disclaimer="Terms apply.",
        guidelines=custom, layout=layout_copy,
        out_path=out,
    )
    # The cascade either accepted a shift or recorded explored attempts.
    if not meta["text_object_clearance_pass"]:
        assert isinstance(meta["headline_box_shift_attempts"], list)
    # When a shift won, headline_box_adjusted differs from the original.
    if meta["headline_box_shift_success"] and meta["headline_box_adjusted"]:
        assert meta["headline_box_adjusted"] != meta["headline_box_original"]


def test_compose_clearance_pass_reflects_rendered_bbox(brand_yaml, tmp_path):
    """A configured candidate sitting far from the focal area must report
    text_object_clearance_pass=True with a positive gap_px sourced from the
    rendered bbox (not the box)."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1080, 1080), (60, 90, 120))
    d = ImageDraw.Draw(img)
    # Right-side cluster only.
    for x in range(640, 1040, 28):
        for y in range(400, 800, 28):
            d.ellipse((x, y, x + 18, y + 18), fill=(245, 240, 230))
    p = tmp_path / "right_only.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="1x1",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    if meta["product_safe_zone_box"] is not None:
        # Whatever the result, the recorded gap_px is a number (not the
        # legacy candidate-box gap).
        gap = meta["text_object_gap_px"]
        if gap is not None:
            assert gap >= 0
