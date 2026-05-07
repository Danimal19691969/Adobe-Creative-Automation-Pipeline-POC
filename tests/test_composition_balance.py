"""Final composition balance pass.

Locks in the four hero-feel fixes layered on top of the art-director pass:
  - balanced-wrap helper avoids one-word lines on narrow boxes;
  - the fitter prefers balanced wrap over greedy when greedy is choppy;
  - adaptive box widening extends the chosen candidate when the focal
    cluster sits far from it;
  - the letterbox fallback fits the source onto a solid neutral canvas
    when no crop offset clears the focal cluster from the canvas edges,
    with a brand-color snap on the fill color.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw, ImageFont

from creative_pipeline.schemas import (
    BrandGuidelines,
    LayoutTemplate,
    LogoPlacement,
    PerAspectLayout,
)
from creative_pipeline.tools.pillow_composer import (
    _fit_text_with_pixel_bounds,
    _letterbox_fit_fallback,
    _sample_edge_median_color,
    _snap_color_to_brand,
    _widen_box_toward_focal_safe_zone,
    _wrap_text_balanced,
    compose_creative,
    smart_crop_with_scoring,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


# ---------- Fix 1: balanced wrap -----------------------------------------


def _draw() -> ImageDraw.ImageDraw:
    return ImageDraw.Draw(Image.new("RGB", (2000, 2000)))


def test_balanced_wrap_avoids_single_word_lines():
    """Narrow box where greedy wraps to single words → balanced split keeps
    multi-word lines while still fitting the box. At 58 px in a 410-px
    box the demo headline can be split as
    ``["Refresh your", "summer,", "naturally."]``."""
    draw = _draw()
    font = ImageFont.truetype("fonts/Montserrat-Bold.ttf", 58)
    lines = _wrap_text_balanced(
        draw, "Refresh your summer, naturally.",
        font=font, max_width=410, target_line_count=3,
    )
    assert lines is not None
    assert len(lines) == 3
    # At least one line should be a multi-word phrase (proof the function
    # didn't just return the greedy 1-word/line shape).
    assert sum(1 for line in lines if len(line.split()) >= 2) >= 1


def test_balanced_wrap_returns_none_at_high_font():
    """At 70 px the demo headline can't fit any 3-line balanced split in
    a 410-px box — balanced returns None and the fitter falls through
    to a smaller font where a balanced split exists."""
    draw = _draw()
    font = ImageFont.truetype("fonts/Montserrat-Bold.ttf", 70)
    lines = _wrap_text_balanced(
        draw, "Refresh your summer, naturally.",
        font=font, max_width=410, target_line_count=3,
    )
    assert lines is None


def test_balanced_wrap_returns_none_when_no_split_fits():
    """At a box width too narrow for any 2-line split, balanced returns
    None so the fitter can fall back to greedy or a smaller font size."""
    draw = _draw()
    font = ImageFont.truetype("fonts/Montserrat-Bold.ttf", 100)
    lines = _wrap_text_balanced(
        draw, "Refresh your summer, naturally.",
        font=font, max_width=200, target_line_count=2,
    )
    assert lines is None


def test_balanced_wrap_minimizes_width_spread():
    """Among several valid splits, balanced picks the most width-uniform."""
    draw = _draw()
    font = ImageFont.truetype("fonts/Montserrat-Bold.ttf", 60)
    lines = _wrap_text_balanced(
        draw, "Refresh your summer, naturally.",
        font=font, max_width=600, target_line_count=2,
    )
    assert lines is not None
    assert len(lines) == 2
    # Widths shouldn't be wildly imbalanced.
    widths = []
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=font)
        widths.append(bb[2] - bb[0])
    # Most balanced split for "Refresh your summer, naturally." into 2
    # lines is "Refresh your summer," / "naturally." — that has spread
    # under 350 px at 60px font; greedy would put 1 word on line 2.
    assert max(widths) - min(widths) < max(widths) * 0.6


def test_fitter_prefers_balanced_when_greedy_has_single_word_lines():
    """When the box is wide enough that a non-choppy 2-line balanced
    wrap exists at SOME font size, the fitter picks it over the
    largest-fits-but-choppy alternative. Box width 720 px lets a
    balanced 2-line wrap (`"Refresh your summer,"` / `"naturally."`)
    fit at small-medium font, while greedy at larger sizes goes
    one-word-per-line."""
    draw = _draw()
    font, lines, _h, _r, wrap_strategy = _fit_text_with_pixel_bounds(
        draw, "Refresh your summer, naturally.",
        font_filename="Montserrat-Bold.ttf",
        fonts_dir="fonts",
        min_size_px=40,
        max_size_px=118,
        box_w=720,
        target_h=600,
        preferred_line_count=2,
        min_line_count=2,
        max_line_count=4,
        hero_scale_threshold_px=999,  # disable hero-scale tier
    )
    # Balanced 2-line ("Refresh your summer," + "naturally.") fits
    # comfortably at ~50–55 px in a 720-px box. The fitter must pick
    # a 0-single-word-line candidate when one exists.
    single_word_lines = sum(1 for line in lines if len(line.split()) == 1)
    assert single_word_lines == 0, (
        f"fitter picked a choppy candidate when a clean alternative "
        f"existed: lines={lines}, strategy={wrap_strategy}, "
        f"size={font.size}"
    )


def test_fitter_picks_hero_scale_single_word_when_no_clean_alternative():
    """Narrow box where every size produces single-word lines → fitter
    prefers a hero-scale font (≥ threshold) over a caption-sized one."""
    draw = _draw()
    font, lines, _h, _r, _w = _fit_text_with_pixel_bounds(
        draw, "Refresh your summer, naturally.",
        font_filename="Montserrat-Bold.ttf",
        fonts_dir="fonts",
        min_size_px=52,
        max_size_px=110,
        box_w=350,        # 4 single-word lines at every size
        target_h=600,
        preferred_line_count=2,
        min_line_count=1,
        max_line_count=4,
        hero_scale_threshold_px=80,
    )
    assert font.size >= 80, (
        f"expected hero-scale font (≥80px) when single-word lines "
        f"unavoidable; got {font.size}px"
    )


# ---------- Fix 2: adaptive widening -------------------------------------


def test_widen_extends_box_when_focal_far_right():
    box = (0.07, 0.22, 0.45, 0.44)
    # Focal cluster on the right third (briefed position) leaves room.
    focal_safe = (0.66, 0.05, 0.95, 0.95)
    new_box, reason, delta = _widen_box_toward_focal_safe_zone(
        box, focal_safe, min_gap_px=56,
        canvas_w=1080, canvas_h=1920,
        max_widen_pct=0.18,
    )
    assert new_box != box
    assert new_box[2] > box[2]
    assert "widened" in reason
    assert delta > 0


def test_widen_skips_when_focal_already_close():
    box = (0.07, 0.22, 0.45, 0.44)
    # Focal cluster right against the box (briefed-tight scenario).
    focal_safe = (0.46, 0.05, 0.95, 0.95)
    new_box, reason, _ = _widen_box_toward_focal_safe_zone(
        box, focal_safe, min_gap_px=56,
        canvas_w=1080, canvas_h=1920,
        max_widen_pct=0.18,
    )
    assert new_box == box
    assert "already_wide_enough" in reason


def test_widen_capped_by_max_pct():
    """A pathologically far-right focal cluster shouldn't let the box
    widen past the configured cap."""
    box = (0.07, 0.22, 0.30, 0.44)
    focal_safe = (0.95, 0.0, 1.0, 1.0)
    new_box, _, delta = _widen_box_toward_focal_safe_zone(
        box, focal_safe, min_gap_px=56,
        canvas_w=1080, canvas_h=1920,
        max_widen_pct=0.05,  # cap at 5%
    )
    # Box widened by at most max_widen_pct.
    assert delta <= 0.05 + 1e-3
    assert new_box[2] <= box[2] + 0.05 + 1e-3


def test_widen_disabled_when_max_widen_zero():
    box = (0.07, 0.22, 0.45, 0.44)
    focal_safe = (0.66, 0.05, 0.95, 0.95)
    new_box, reason, delta = _widen_box_toward_focal_safe_zone(
        box, focal_safe, min_gap_px=56,
        canvas_w=1080, canvas_h=1920,
        max_widen_pct=0.0,
    )
    assert new_box == box
    assert reason == "disabled"
    assert delta == 0.0


def test_widen_no_focal_safe_zone_returns_original():
    box = (0.07, 0.22, 0.45, 0.44)
    new_box, reason, _ = _widen_box_toward_focal_safe_zone(
        box, focal_safe_zone_pct=None, min_gap_px=56,
        canvas_w=1080, canvas_h=1920,
        max_widen_pct=0.18,
    )
    assert new_box == box
    assert "no_focal_safe_zone" in reason


# ---------- Fix 3: letterbox fallback ------------------------------------


def test_letterbox_fit_fallback_pads_correctly():
    img = Image.new("RGB", (1000, 1000), (255, 0, 0))
    out, meta = _letterbox_fit_fallback(
        img, target_w=1080, target_h=1920,
        fill_rgb=(0, 100, 200), pad_pct=0.06,
    )
    assert out.size == (1080, 1920)
    # Pad fraction recorded.
    assert meta["pad_pct"] == 0.06
    # The corner pixel must be the fill color (not source red).
    assert out.convert("RGB").getpixel((10, 10)) == (0, 100, 200)


def test_snap_color_to_brand_within_threshold(brand_yaml):
    """A color near brand primary should snap to it."""
    palette = [
        brand_yaml.visual_identity.primary_color,
        brand_yaml.visual_identity.secondary_color,
        brand_yaml.visual_identity.accent_color,
    ]
    # Brand primary is #00B4D8 = (0, 180, 216) — perturb by ~10/channel.
    near_primary = (10, 180, 220)
    snapped, dist = _snap_color_to_brand(near_primary, palette, threshold=60)
    assert snapped == brand_yaml.visual_identity.primary_color
    assert dist < 60


def test_snap_color_to_brand_outside_threshold(brand_yaml):
    """A garish off-brand color should NOT snap."""
    palette = [
        brand_yaml.visual_identity.primary_color,
        brand_yaml.visual_identity.secondary_color,
        brand_yaml.visual_identity.accent_color,
    ]
    far_off = (255, 0, 0)
    snapped, _ = _snap_color_to_brand(far_off, palette, threshold=50)
    assert snapped is None


def test_sample_edge_median_color_returns_uniform_color():
    """Edge sampling on a uniform image returns that color."""
    img = Image.new("RGB", (400, 400), (90, 130, 160))
    rgb = _sample_edge_median_color(img)
    assert rgb == (90, 130, 160)


def _make_full_canvas_focal(tmp_path: Path) -> str:
    """Source image whose dense object cluster spans the entire canvas
    (every edge clipped no matter the crop offset)."""
    img = Image.new("RGB", (1080, 1080), (240, 240, 240))
    d = ImageDraw.Draw(img)
    # Pack high-contrast shapes across the WHOLE canvas, including the
    # extreme corners so every focal-cluster bbox candidate touches an edge.
    for x in range(0, 1080, 24):
        for y in range(0, 1080, 24):
            d.ellipse((x, y, x + 14, y + 14), fill=(40, 60, 100))
    p = tmp_path / "full_canvas.png"
    img.save(p)
    return str(p)


def test_letterbox_applied_when_clip_unavoidable_and_enabled(brand_yaml, tmp_path):
    """Source whose focal cluster spans the entire canvas → letterbox
    applies (when explicitly enabled by the layout). Letterbox is OFF
    by default for this brand because matte borders read as "weird
    graphic boxes" on social campaigns; this test opts in via a custom
    layout to verify the code still works for brands that want it."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.enable_focal_letterbox_when_clip_unavoidable = True

    src = Image.open(_make_full_canvas_focal(tmp_path)).convert("RGB")
    cropped, meta = smart_crop_with_scoring(
        src, target_w=1920, target_h=1080,
        layout=layout, ratio="16x9", brand=custom,
    )
    assert cropped.size == (1920, 1080)
    assert meta["letterbox_applied"] is True
    assert meta["letterbox_pad_pct"] is not None
    assert meta["letterbox_pad_pct"] > 0
    assert meta["letterbox_color_used"] is not None


def test_letterbox_default_off_in_demo_brand(brand_yaml, tmp_path):
    """The shipped demo brand disables letterbox so social campaign
    creatives don't ship with matte borders. Even when every crop
    candidate clips the focal, the canvas stays full-bleed photography
    and the issue surfaces via composition_warnings."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    assert layout.enable_focal_letterbox_when_clip_unavoidable is False

    src = Image.open(_make_full_canvas_focal(tmp_path)).convert("RGB")
    _, meta = smart_crop_with_scoring(
        src, target_w=1920, target_h=1080,
        layout=layout, ratio="16x9", brand=brand_yaml,
    )
    assert meta["letterbox_applied"] is False


def test_letterbox_skipped_when_disabled_in_layout(brand_yaml, tmp_path):
    """When the layout doesn't opt-in, no letterbox even if every crop clips."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.enable_focal_letterbox_when_clip_unavoidable = False

    src = Image.open(_make_full_canvas_focal(tmp_path)).convert("RGB")
    _, meta = smart_crop_with_scoring(
        src, target_w=1920, target_h=1080,
        layout=layout, ratio="16x9", brand=custom,
    )
    assert meta["letterbox_applied"] is False
    assert meta["letterbox_pad_pct"] is None


def test_letterbox_skipped_when_pad_exceeds_cap(brand_yaml, tmp_path):
    """When the required pad to clear focal exceeds letterbox_max_pad_pct,
    the composer falls back to the legacy clip behavior."""
    custom = brand_yaml.model_copy(deep=True)
    layout = custom.layout_templates["premium_product_hero"]
    layout.letterbox_max_pad_pct = 0.005  # absurdly tight cap

    src = Image.open(_make_full_canvas_focal(tmp_path)).convert("RGB")
    _, meta = smart_crop_with_scoring(
        src, target_w=1920, target_h=1080,
        layout=layout, ratio="16x9", brand=custom,
    )
    assert meta["letterbox_applied"] is False


# ---------- end-to-end --------------------------------------------------


def test_compose_records_all_new_audit_fields(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1920, 1080), (90, 110, 140))
    p = tmp_path / "clean.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="9x16",
        headline="Refresh your summer, naturally.", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    for k in (
        "headline_wrap_strategy",
        "headline_box_widened",
        "headline_box_width_delta_pct",
        "headline_box_pre_widen_pct",
        "headline_widen_reason",
        "letterbox_applied",
        "letterbox_pad_pct",
        "letterbox_color_used",
        "letterbox_color_source",
    ):
        assert k in meta, f"missing meta field: {k}"


def test_demo_headline_avoids_tiny_choppy_render(brand_yaml, tmp_path):
    """End-to-end check on the demo headline + 9x16: the composer must
    NEVER ship a tiny multi-line stack (e.g. 4 single-word lines at
    60 px). Acceptable outputs are:
      (a) ≤ 3 lines at any size — clean multi-word wrap fits, OR
      (b) ≥ ``headline_hero_scale_threshold_px`` font when the wrap is
          forced to single-word lines (poster typography).
    A 4-line render at ≤ 70 px reads as caption-sized choppy and is a
    regression."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    img = Image.new("RGB", (1080, 1920), (90, 110, 140))
    p = tmp_path / "clean.png"
    img.save(p)
    out = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=str(p), ratio="9x16",
        headline="Refresh your summer, naturally.", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out,
    )
    line_count = meta["headline_line_count"]
    size = meta["headline_size_px"]
    threshold = layout.headline_hero_scale_threshold_px
    acceptable = line_count <= 3 or size >= threshold
    assert acceptable, (
        f"caption-sized choppy headline: lines={line_count}, size={size}px, "
        f"hero_threshold={threshold}, wrap={meta['headline_wrap_strategy']}"
    )
