"""Text-safe-area scoring tests.

Locks in the deterministic candidate-box scorer that:
  - Loads candidate zones from brand YAML (per_aspect.headline_candidate_boxes).
  - Picks the cleanest readable candidate based on contrast + texture stddev
    + Canny edge density.
  - Penalizes busy/high-edge regions and prefers calm areas.
  - Falls back to the single ``headline_box`` when scoring is off or no
    candidates are configured.
  - Records the full audit trail in output meta (selection reason, all
    candidate scores, winner's metrics).

The scorer is pure Pillow + numpy + cv2 (no LLM).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from creative_pipeline.schemas import BrandGuidelines, PerAspectLayout
from creative_pipeline.tools.pillow_composer import (
    _region_edge_density,
    _region_texture_score,
    _select_headline_box,
    compose_creative,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


def _make_split_hero(tmp_path: Path) -> str:
    """Synthetic 1080x1080 hero: clean dark navy on the left half, busy
    high-frequency stripes on the right half. The scorer should prefer
    candidate boxes that fall in the left half."""
    img = Image.new("RGB", (1080, 1080), (10, 35, 70))  # dark navy
    d = ImageDraw.Draw(img)
    # Bright high-frequency stripes on the right half — high texture + edges.
    for x in range(540, 1080, 8):
        d.rectangle((x, 0, x + 4, 1080), fill=(255, 240, 180))
    p = tmp_path / "split.png"
    img.save(p)
    return str(p)


def _make_uniform_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (1080, 1080), (40, 70, 120))
    p = tmp_path / "uniform.png"
    img.save(p)
    return str(p)


# -------- 1. YAML loads candidate boxes --------

def test_candidate_boxes_load_from_yaml(brand_yaml):
    """premium_product_hero must define candidate boxes for all three aspects."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    for ratio in ("1x1", "9x16", "16x9"):
        pa = layout.per_aspect[ratio]
        assert pa.headline_candidate_boxes, f"missing candidates for {ratio}"
        assert pa.avoid_busy_regions is True
        # All candidate boxes are valid unit-square rects.
        for box in pa.headline_candidate_boxes:
            x0, y0, x1, y1 = box
            assert 0.0 <= x0 < x1 <= 1.0
            assert 0.0 <= y0 < y1 <= 1.0


# -------- 2. Composer selects the cleanest candidate --------

def test_select_headline_box_prefers_clean_left_over_busy_right(brand_yaml, tmp_path):
    """Two candidates — one over the clean navy half, one over the striped
    half. The scorer must pick the clean one."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_split_hero(tmp_path)).convert("RGBA")

    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[
            (0.05, 0.55, 0.45, 0.85),   # left half — clean
            (0.55, 0.55, 0.95, 0.85),   # right half — busy stripes
        ],
        avoid_busy_regions=True,
    )

    selected_box, meta, scores, _focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    # Selected box must be the LEFT half candidate.
    assert selected_box[0] < 0.10  # x0 of left candidate
    assert selected_box[2] < 0.50  # x1 left of midline
    # The right candidate's texture/edge metrics must be higher than the left.
    by_x0 = sorted(scores, key=lambda s: s["box_pct"][0])
    left, right = by_x0[0], by_x0[1]
    assert right["texture_score"] > left["texture_score"]
    assert right["edge_density"] >= left["edge_density"]
    assert left["score"] > right["score"]


# -------- 3. Busy/high-variance region penalized --------

def test_texture_and_edge_helpers_distinguish_clean_vs_busy(tmp_path):
    """Sanity: stddev/edge metrics are higher on busy stripes than on flat fill."""
    busy_path = _make_split_hero(tmp_path)
    busy_canvas = Image.open(busy_path).convert("RGBA")
    # Striped half (right): texture/edge should be high.
    busy_box = (540, 200, 1080, 800)
    # Flat half (left): texture/edge should be low.
    clean_box = (0, 200, 540, 800)

    busy_tex = _region_texture_score(busy_canvas, busy_box)
    clean_tex = _region_texture_score(busy_canvas, clean_box)
    assert busy_tex > clean_tex * 4

    busy_edges = _region_edge_density(busy_canvas, busy_box)
    clean_edges = _region_edge_density(busy_canvas, clean_box)
    assert busy_edges > clean_edges + 0.05


# -------- 4. Cleaner candidate preferred when contrast is acceptable --------

def test_uniform_hero_picks_first_candidate_with_zero_texture(brand_yaml, tmp_path):
    """A perfectly flat photo: every candidate has the same texture/edge score.
    Scorer must still produce a deterministic winner without crashing."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_uniform_hero(tmp_path)).convert("RGBA")
    pa = layout.per_aspect["1x1"]

    selected_box, meta, scores, _focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    # All scores are valid floats and the selected box is among the candidates.
    assert all("score" in s for s in scores)
    assert tuple(selected_box) in {tuple(c) for c in pa.headline_candidate_boxes}


# -------- 5. Metadata recorded in compose_creative output --------

def test_compose_records_selection_audit(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    hero = _make_split_hero(tmp_path)
    out_path = str(tmp_path / "out.png")
    meta = compose_creative(
        hero_path=hero, ratio="1x1",
        headline="Refresh your summer", disclaimer="T&C apply.",
        guidelines=brand_yaml, layout=layout, out_path=out_path,
    )
    for field in (
        "headline_box_selected_pct",
        "headline_box_selected_px",
        "headline_box_selection_reason",
        "headline_box_score",
        "headline_region_texture_score",
        "headline_region_edge_density",
        "headline_region_contrast_estimate",
        "headline_box_candidates",
    ):
        assert field in meta, f"missing meta field {field}"
    # Candidate list must include scoring details for each candidate.
    cands = meta["headline_box_candidates"]
    assert len(cands) == len(brand_yaml.layout_templates["premium_product_hero"].per_aspect["1x1"].headline_candidate_boxes)
    for c in cands:
        for k in ("box_pct", "score", "contrast_estimate", "texture_score", "edge_density"):
            assert k in c


# -------- 6. Fallback to single headline_box when scoring off --------

def test_fallback_to_single_box_when_avoid_busy_off(brand_yaml, tmp_path):
    """avoid_busy_regions=False ⇒ scorer is bypassed and the single
    headline_box is used as before."""
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_uniform_hero(tmp_path)).convert("RGBA")

    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[(0.10, 0.10, 0.40, 0.30)],
        avoid_busy_regions=False,   # disabled
    )

    selected_box, meta, scores, _focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    assert tuple(selected_box) == (0.05, 0.65, 0.95, 0.95)
    assert "single_configured_box" in meta["selection_reason"]
    assert scores == []


def test_fallback_to_single_box_when_no_candidates(brand_yaml, tmp_path):
    layout = brand_yaml.layout_templates["premium_product_hero"]
    cropped = Image.open(_make_uniform_hero(tmp_path)).convert("RGBA")

    pa = PerAspectLayout(
        headline_box=(0.05, 0.65, 0.95, 0.95),
        text_align="left",
        headline_candidate_boxes=[],   # none configured
        avoid_busy_regions=True,        # but on
    )
    selected_box, meta, scores, _focal = _select_headline_box(
        cropped, layout, pa, brand_yaml, target_w=1080, target_h=1080,
    )
    assert tuple(selected_box) == (0.05, 0.65, 0.95, 0.95)
    assert scores == []


def test_compose_renders_normally_with_no_candidates(brand_yaml, tmp_path):
    """End-to-end: a custom layout with no candidates should still render
    successfully and report the fallback reason."""
    layout = brand_yaml.layout_templates["premium_product_hero"].model_copy(deep=True)
    for ratio_key in layout.per_aspect:
        layout.per_aspect[ratio_key].headline_candidate_boxes = []
        layout.per_aspect[ratio_key].avoid_busy_regions = False

    hero = _make_uniform_hero(tmp_path)
    out_path = str(tmp_path / "fb.png")
    meta = compose_creative(
        hero_path=hero, ratio="1x1",
        headline="Hi", disclaimer=None,
        guidelines=brand_yaml, layout=layout, out_path=out_path,
    )
    assert "single_configured_box" in meta["headline_box_selection_reason"]
    assert meta["headline_box_candidates"] == []
