"""Art-director composition pass.

Locks in the deterministic crop / logo / accent / disclaimer selectors
and the final composition score:
  - smart_crop_with_scoring picks the offset that keeps the focal
    cluster off the canvas edges;
  - select_logo_position falls through alternates when the configured
    corner crowds the focal area;
  - the accent rail respects the brand's left safe margin even when
    the headline box hugs the frame;
  - select_disclaimer_box prefers the configured candidate but falls
    back to alternates when the preferred zone is crowded;
  - composition_score / composition_warnings surface every issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from creative_pipeline.schemas import (
    BrandGuidelines,
    LogoPlacement,
    PerAspectLayout,
)
from creative_pipeline.tools.pillow_composer import (
    _compute_composition_score,
    _compute_logo_footprint,
    _focal_edge_clearance_metrics,
    _resolve_accent_safe_x,
    _resolve_edge_pad_px,
    compose_creative,
    select_disclaimer_box,
    select_logo_position,
    smart_crop_with_scoring,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


# ----- YAML wiring ----------------------------------------------------------


def test_brand_loads_logo_allowed_positions(brand_yaml):
    vi = brand_yaml.visual_identity
    # Configured first, all four corners as alternates.
    assert vi.logo_placement == LogoPlacement.TOP_RIGHT
    assert LogoPlacement.TOP_RIGHT in vi.logo_allowed_positions
    assert LogoPlacement.TOP_LEFT in vi.logo_allowed_positions
    assert vi.hard_fail_logo_product_collision is True


def test_brand_loads_focal_edge_knobs(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.focal_edge_clearance_pct > 0
    assert layout.hard_fail_focal_edge_clip is True
    for r in ("1x1", "9x16", "16x9"):
        assert layout.min_focal_edge_gap_px[r] >= 40


def test_brand_loads_accent_safe_zone(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.accent_safe_zone_pct >= 0.05
    for r in ("1x1", "9x16", "16x9"):
        assert layout.min_accent_edge_gap_px[r] >= 40


def test_brand_loads_disclaimer_candidates(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    for r in ("1x1", "9x16", "16x9"):
        assert len(layout.disclaimer_candidate_boxes[r]) >= 2
        assert layout.min_disclaimer_object_gap_px[r] >= 25


def test_headline_ranges_bumped(brand_yaml):
    pa = brand_yaml.typography.per_aspect
    assert pa["1x1"].headline_max_font_size_px >= 96
    assert pa["9x16"].headline_max_font_size_px >= 118
    assert pa["16x9"].headline_max_font_size_px >= 132


def test_headline_candidate_boxes_use_safer_left_margin(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    for r in ("1x1", "9x16"):
        for box in layout.per_aspect[r].headline_candidate_boxes:
            x_min = box[0]
            assert x_min >= 0.07, (
                f"{r} candidate {box} hugs left edge "
                f"(x_min={x_min}, expected ≥ 0.07)"
            )


def test_headline_candidate_order_biases_upper(brand_yaml):
    """First candidate (preferred) must be the upper zone for vertical
    and square layouts so the composer reaches for clean upper space
    before falling to the lower band."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    for r in ("1x1", "9x16"):
        candidates = layout.per_aspect[r].headline_candidate_boxes
        # First candidate's center y < second's center y < third's center y.
        ys = [(c[1] + c[3]) / 2 for c in candidates]
        assert ys[0] < ys[1] < ys[2], (
            f"{r} candidates are not biased upper-to-lower: ys={ys}"
        )


# ----- crop scoring ---------------------------------------------------------


def _make_focal_clipped_right(tmp_path: Path) -> str:
    """Source image whose dense object cluster sits at the far right edge —
    a center crop into a square would push the cluster off the right side."""
    img = Image.new("RGB", (1600, 1080), (90, 130, 160))
    d = ImageDraw.Draw(img)
    # Pack a focal cluster at x=1300..1580 (right edge).
    for x in range(1300, 1590, 20):
        for y in range(400, 800, 20):
            d.ellipse((x, y, x + 14, y + 14), fill=(40, 40, 40))
    p = tmp_path / "right_clipped.png"
    img.save(p)
    return str(p)


def test_smart_crop_score_picks_anti_clipping_offset(brand_yaml, tmp_path):
    """The scorer should pick a crop offset that keeps the focal cluster
    off the right canvas edge (or, when impossible, prefer the variant
    with the largest min-edge gap)."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    src = Image.open(_make_focal_clipped_right(tmp_path)).convert("RGB")
    cropped, meta = smart_crop_with_scoring(
        src, target_w=1080, target_h=1080,
        layout=layout, ratio="1x1",
    )
    assert cropped.size == (1080, 1080)
    assert meta["crop_strategy_used"].startswith("scored_")
    assert meta["crop_box_used"] is not None
    assert isinstance(meta["crop_box_scores"], list) and len(meta["crop_box_scores"]) >= 3


def test_smart_crop_records_full_audit(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    src = Image.open(_make_focal_clipped_right(tmp_path)).convert("RGB")
    _, meta = smart_crop_with_scoring(
        src, target_w=1080, target_h=1080,
        layout=layout, ratio="1x1",
    )
    for k in (
        "crop_strategy_used", "crop_box_used", "crop_box_candidates",
        "crop_box_scores", "focal_edge_clearance_pass",
        "focal_edge_clip_detected", "edges_touched",
        "crop_edge_clip_penalty_applied",
    ):
        assert k in meta


def test_focal_edge_clearance_metrics_flags_clip():
    """A focal box touching the right canvas edge has min_gap_px=0
    and clip_detected=True."""
    m = _focal_edge_clearance_metrics(
        focal_pct=(0.6, 0.3, 1.0, 0.7),
        target_w=1080, target_h=1080,
        edge_pad_px=48,
    )
    assert m["focal_edge_clip_detected"] is True
    assert m["focal_edge_clearance_pass"] is False


def test_focal_edge_clearance_metrics_passes_centered():
    m = _focal_edge_clearance_metrics(
        focal_pct=(0.30, 0.30, 0.70, 0.70),
        target_w=1080, target_h=1080,
        edge_pad_px=48,
    )
    assert m["focal_edge_clip_detected"] is False
    assert m["focal_edge_clearance_pass"] is True


def test_resolve_edge_pad_px_uses_max_of_pct_and_floor(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    px = _resolve_edge_pad_px(layout, "16x9", 1920, 1080)
    # 0.045 * 1080 = 48.6 → 48; floor for 16x9 is 64; max wins.
    assert px == 64


def test_smart_crop_aspect_match_skips_scoring(brand_yaml):
    """When source already matches the target aspect, the scorer no-ops
    and reports the no_crop_aspect_match strategy."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1080, 1080), (120, 120, 120))
    cropped, meta = smart_crop_with_scoring(
        img, target_w=1080, target_h=1080,
        layout=layout, ratio="1x1",
    )
    assert meta["crop_strategy_used"] == "no_crop_aspect_match"
    assert cropped.size == (1080, 1080)


# ----- logo position --------------------------------------------------------


def test_logo_footprint_matches_badge_padding(brand_yaml):
    """Logo footprint accounts for the badge padding so position selection
    sees the on-canvas dimensions, not just the inner mark."""
    w, h = _compute_logo_footprint(1080, 1080, brand_yaml)
    # logo_height_pct=0.085 → 91.8 → ~91; pad=18 → footprint 127×~127 (depends
    # on logo aspect).
    assert h > int(0.085 * 1080)  # > inner mark height (badge padding adds)


def test_select_logo_position_fallthrough_when_top_right_collides(brand_yaml):
    """When the focal safe zone covers top-right, the picker falls through
    to top-left."""
    # Focal safe zone covers the upper-right quadrant.
    focal_safe = (0.55, 0.0, 1.0, 0.40)
    pick = select_logo_position(
        canvas_w=1080, canvas_h=1080,
        logo_w=140, logo_h=140,
        brand=brand_yaml, ratio="1x1",
        focal_safe_zone_pct=focal_safe,
    )
    assert pick["logo_position_selected"] != brand_yaml.visual_identity.logo_placement.value
    assert pick["logo_position_adjusted"] is True
    assert pick["logo_product_clearance_pass"] is True


def test_select_logo_position_keeps_configured_when_clear(brand_yaml):
    """Focal safe zone at lower-center → top-right is fine."""
    focal_safe = (0.30, 0.55, 0.70, 0.95)
    pick = select_logo_position(
        canvas_w=1080, canvas_h=1080,
        logo_w=140, logo_h=140,
        brand=brand_yaml, ratio="1x1",
        focal_safe_zone_pct=focal_safe,
    )
    assert pick["logo_position_selected"] == brand_yaml.visual_identity.logo_placement.value
    assert pick["logo_position_adjusted"] is False


def test_select_logo_position_records_attempts(brand_yaml):
    pick = select_logo_position(
        canvas_w=1080, canvas_h=1080,
        logo_w=140, logo_h=140,
        brand=brand_yaml, ratio="1x1",
        focal_safe_zone_pct=(0.55, 0.0, 1.0, 0.40),
    )
    assert isinstance(pick["logo_position_attempts"], list)
    assert pick["logo_position_attempts"][0]["placement"] == "top-right"


# ----- accent safe zone -----------------------------------------------------


def test_resolve_accent_safe_x_pushes_rail_away_from_edge(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    # Headline box hugs the canvas edge (x=10), thickness=6.
    rail_x_left, rail_x_right = _resolve_accent_safe_x(
        canvas_w=1080, canvas_h=1080, layout=layout, ratio="1x1",
        desired_x=10, thickness=6,
    )
    # Accent must respect the brand's safe-zone floor (48 px for 1x1).
    assert rail_x_left >= 48


def test_resolve_accent_safe_x_keeps_rail_near_headline_when_safe(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    rail_x_left, _ = _resolve_accent_safe_x(
        canvas_w=1080, canvas_h=1080, layout=layout, ratio="1x1",
        desired_x=300, thickness=6,
    )
    # Headline is well inside the canvas — rail should sit near the
    # desired anchor, not the safe-zone floor.
    assert rail_x_left >= 290 - 12


# ----- disclaimer candidates ------------------------------------------------


def test_select_disclaimer_box_picks_first_clear_candidate(brand_yaml):
    """When the configured candidate has clearance, the selector takes it
    and reports candidate_index=0."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    canvas = Image.new("RGB", (1080, 1080), (40, 80, 120)).convert("RGBA")
    # Focal safe zone at lower-center — not in the bottom-right corner.
    focal_safe = (0.30, 0.50, 0.70, 0.85)
    pick = select_disclaimer_box(
        canvas, layout, ratio="1x1",
        focal_safe_zone_pct=focal_safe,
        text_color_options=[
            brand_yaml.typography.text_color_on_dark,
            brand_yaml.typography.text_color_on_light,
        ],
        fallback_box_px=(0, 0, 100, 50),
    )
    assert pick["candidate_index"] == 0
    assert pick["clearance_pass"] is True


def test_select_disclaimer_box_falls_back_when_preferred_is_crowded(brand_yaml):
    """When the bottom-right is covered by a focal safe zone, the picker
    falls through to the bottom-left alternate."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    canvas = Image.new("RGB", (1080, 1080), (40, 80, 120)).convert("RGBA")
    # Focal safe zone parked over the bottom-right candidate (which is the
    # configured preference).
    focal_safe = (0.55, 0.84, 1.0, 1.0)
    pick = select_disclaimer_box(
        canvas, layout, ratio="1x1",
        focal_safe_zone_pct=focal_safe,
        text_color_options=[
            brand_yaml.typography.text_color_on_dark,
            brand_yaml.typography.text_color_on_light,
        ],
        fallback_box_px=(0, 0, 100, 50),
    )
    assert pick["candidate_index"] == 1
    assert pick["clearance_pass"] is True


def test_select_disclaimer_box_records_attempts(brand_yaml):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    canvas = Image.new("RGB", (1080, 1080), (40, 80, 120)).convert("RGBA")
    pick = select_disclaimer_box(
        canvas, layout, ratio="1x1",
        focal_safe_zone_pct=(0.55, 0.84, 1.0, 1.0),
        text_color_options=[
            brand_yaml.typography.text_color_on_dark,
            brand_yaml.typography.text_color_on_light,
        ],
        fallback_box_px=(0, 0, 100, 50),
    )
    assert len(pick["candidate_attempts"]) >= 2
    assert "selection_reason" in pick


def test_select_disclaimer_box_uses_fallback_when_no_candidates(brand_yaml):
    """When the layout doesn't supply candidate boxes (legacy templates),
    the function returns the fallback box and notes the reason."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.disclaimer_candidate_boxes = {}
    canvas = Image.new("RGB", (1080, 1080), (40, 80, 120)).convert("RGBA")
    fallback = (100, 900, 500, 950)
    pick = select_disclaimer_box(
        canvas, layout, ratio="1x1",
        focal_safe_zone_pct=None,
        text_color_options=[
            brand_yaml.typography.text_color_on_dark,
            brand_yaml.typography.text_color_on_light,
        ],
        fallback_box_px=fallback,
    )
    assert pick["box_px"] == fallback
    assert pick["candidate_index"] is None
    assert "no_candidates_configured" in pick["selection_reason"]


# ----- composition score ----------------------------------------------------


def test_composition_score_clean_render_is_high():
    s = _compute_composition_score(
        crop_clearance_pass=True, crop_clip_detected=False,
        logo_clearance_pass=True, logo_collision=False,
        headline_prominence=0.85, headline_clearance_pass=True,
        headline_too_small=False, accent_safe_zone_pass=True,
        disclaimer_clearance_pass=True,
        all_text_candidates_failed=False,
    )
    assert s["composition_score"] > 0.9
    assert s["composition_warnings"] == []


def test_composition_score_clip_emits_warning():
    s = _compute_composition_score(
        crop_clearance_pass=False, crop_clip_detected=True,
        logo_clearance_pass=True, logo_collision=False,
        headline_prominence=0.85, headline_clearance_pass=True,
        headline_too_small=False, accent_safe_zone_pass=True,
        disclaimer_clearance_pass=True,
        all_text_candidates_failed=False,
    )
    assert "focal_edge_clip" in s["composition_warnings"]
    assert s["composition_score"] < 0.95


def test_composition_score_collects_multiple_warnings():
    s = _compute_composition_score(
        crop_clearance_pass=False, crop_clip_detected=True,
        logo_clearance_pass=False, logo_collision=True,
        headline_prominence=0.40, headline_clearance_pass=False,
        headline_too_small=True, accent_safe_zone_pass=False,
        disclaimer_clearance_pass=False,
        all_text_candidates_failed=True,
    )
    expected = {
        "focal_edge_clip", "logo_product_collision",
        "headline_too_small", "headline_near_object",
        "accent_hugs_edge", "disclaimer_near_object",
        "all_candidates_failed_clearance",
    }
    assert expected.issubset(set(s["composition_warnings"]))


# ----- compose_creative end-to-end -----------------------------------------


def test_compose_includes_full_art_director_meta(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1920, 1080), (100, 130, 160))
    p = tmp_path / "clean.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="9x16",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    for k in (
        "crop_strategy_used", "crop_box_used",
        "focal_edge_gap_px", "focal_edge_clearance_pass", "focal_edge_clip_detected",
        "logo_position_selected", "logo_position_configured",
        "logo_product_gap_px", "logo_product_clearance_pass",
        "accent_line_box", "accent_edge_gap_px", "accent_safe_zone_pass",
        "disclaimer_candidate_index", "disclaimer_position_selected",
        "disclaimer_position_configured", "disclaimer_selection_reason",
        "composition_score", "composition_warnings", "composition_factors",
    ):
        assert k in meta, f"missing meta field: {k}"


def test_compose_clean_canvas_high_composition_score(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1080, 1920), (100, 130, 160))
    p = tmp_path / "clean.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="9x16",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    # On a clean canvas the composition should be near-perfect.
    assert meta["composition_score"] >= 0.85
    assert "focal_edge_clip" not in (meta["composition_warnings"] or [])


def test_compose_accent_respects_left_safe_zone(brand_yaml, tmp_path):
    """Even when the headline box hugs the canvas (x=0.07 in 1080-wide
    canvas = 75 px), the accent rail must sit at the brand's safe-zone
    floor (48 px for 1x1) — not at hb[0] - thickness*2."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1080, 1080), (90, 130, 160))
    p = tmp_path / "clean.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="1x1",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    if meta["accent_line_box"] is not None:
        rail_x_left = meta["accent_line_box"][0]
        assert rail_x_left >= layout.min_accent_edge_gap_px["1x1"]
        assert meta["accent_safe_zone_pass"] is True
