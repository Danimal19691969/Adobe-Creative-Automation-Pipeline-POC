"""Logo-badge rendering quality tests.

Locks in the supersampled-LANCZOS path that produces a smooth circular badge
edge (versus the direct-draw rounded_rectangle which paints a jagged perimeter
at typical badge sizes around 100-150 px).

Covers:
  - The supersampled helper exists and produces a badge image at the
    requested final size.
  - The badge edge has many intermediate alpha values (anti-aliasing).
  - Calling stamp_logo with logo_treatment=badge invokes the helper.
  - logo_box / logo_position / logo_size_px metadata is unchanged.
  - Plain (non-badge) path still works.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image, ImageDraw

from creative_pipeline.schemas import BrandGuidelines, LogoTreatment
from creative_pipeline.tools.pillow_composer import (
    _render_logo_badge_supersampled,
    stamp_logo,
)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def synthetic_logo() -> Image.Image:
    """A simple cyan circle on transparent background — stands in for the
    real brand logo without depending on its file being present."""
    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((20, 20, 380, 380), fill=(0, 180, 216, 255))
    ImageDraw.Draw(img).text((130, 160), "AQ", fill=(255, 255, 255, 255))
    return img


# -------- Helper produces a smooth-edged badge --------

def test_supersampled_helper_returns_final_size(synthetic_logo):
    badge = _render_logo_badge_supersampled(
        logo_source=synthetic_logo,
        target_w=80, target_h=80, pad=14,
        badge_color_rgba=(255, 255, 255, 184),
    )
    assert badge.size == (80 + 28, 80 + 28)  # target + 2*pad
    assert badge.mode == "RGBA"


def _count_intermediate_alphas(img: Image.Image, ring_thickness: int = 6) -> int:
    """Count distinct alpha values strictly between 0 and 255 in a thin ring
    near the badge perimeter. A jagged-edge badge has 0-or-255 only; an
    anti-aliased one has many gradient values across the perimeter."""
    alpha = img.split()[-1]
    w, h = alpha.size
    cx, cy = w / 2, h / 2
    outer_r = min(w, h) / 2
    inner_r = outer_r - ring_thickness
    seen = set()
    px = alpha.load()
    for y in range(h):
        for x in range(w):
            r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if inner_r <= r <= outer_r + 1:
                a = px[x, y]
                if 0 < a < 255:
                    seen.add(a)
    return len(seen)


def test_badge_edge_is_anti_aliased(synthetic_logo):
    """Supersampled badge must have many distinct intermediate alpha values
    along its perimeter — that's what 'smooth edge' means in pixel terms."""
    badge = _render_logo_badge_supersampled(
        logo_source=synthetic_logo,
        target_w=100, target_h=100, pad=18,
        badge_color_rgba=(255, 255, 255, 184),
    )
    intermediate = _count_intermediate_alphas(badge, ring_thickness=8)
    # A direct-draw rounded_rectangle at this size yields ≤ 2-3 intermediate
    # values (Pillow only does 1px aliasing on the outline). The supersampled
    # path produces dozens.
    assert intermediate >= 20, f"badge edge looks jagged — only {intermediate} intermediate alphas"


# -------- stamp_logo integration --------

def _make_brand_with_synthetic_logo(brand_yaml: BrandGuidelines, tmp_path: Path) -> BrandGuidelines:
    logo_path = tmp_path / "logo.png"
    img = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((20, 20, 380, 380), fill=(0, 180, 216, 255))
    img.save(logo_path)
    custom = brand_yaml.model_copy(deep=True)
    custom.visual_identity.logo_path = str(logo_path)
    return custom


def test_stamp_logo_badge_path_returns_metadata(brand_yaml, tmp_path):
    """logo_box / logo_position / logo_size_px must remain in the meta after
    the supersample refactor — same shape as before."""
    custom = _make_brand_with_synthetic_logo(brand_yaml, tmp_path)
    custom.visual_identity.logo_treatment = LogoTreatment.BADGE
    canvas = Image.new("RGBA", (1080, 1080), (40, 80, 120, 255))
    out, meta = stamp_logo(canvas, custom)

    assert meta["treatment"] == "badge"
    assert isinstance(meta["position"], list) and len(meta["position"]) == 2
    assert isinstance(meta["size_px"], list) and len(meta["size_px"]) == 2
    # Final-size badge is target_h*2 + 2*pad ≈ around 130 px on a 1080 canvas.
    assert 80 <= meta["size_px"][1] <= 200


def test_stamp_logo_badge_edge_anti_aliased_on_canvas(brand_yaml, tmp_path):
    """End-to-end: after stamp_logo, the badge area on the final canvas
    should still show intermediate alpha gradients along the perimeter."""
    custom = _make_brand_with_synthetic_logo(brand_yaml, tmp_path)
    custom.visual_identity.logo_treatment = LogoTreatment.BADGE
    canvas = Image.new("RGBA", (1080, 1080), (0, 0, 0, 255))  # solid black bg
    out, meta = stamp_logo(canvas, custom)

    x, y = meta["position"]
    w, h = meta["size_px"]
    badge_region = out.crop((x, y, x + w, y + h))
    # Convert to L of (R+G+B) to look for non-uniform gradient at the edge —
    # a hard-edged badge would have step-function brightness on the rim.
    luma = badge_region.convert("L")
    px = luma.load()
    cw, ch = luma.size
    cx, cy = cw / 2, ch / 2
    outer_r = min(cw, ch) / 2
    inner_r = outer_r - 6
    edge_lumas = set()
    for yy in range(ch):
        for xx in range(cw):
            r = ((xx - cx) ** 2 + (yy - cy) ** 2) ** 0.5
            if inner_r <= r <= outer_r + 1:
                edge_lumas.add(px[xx, yy])
    # Many distinct luma values along the perimeter ⇒ anti-aliased badge.
    assert len(edge_lumas) >= 20, (
        f"badge perimeter has only {len(edge_lumas)} distinct luma values — "
        "edge probably stair-steps"
    )


def test_stamp_logo_plain_path_still_works(brand_yaml, tmp_path):
    """Non-badge treatment must continue to render the logo at final size
    via single LANCZOS resize, no badge overhead."""
    custom = _make_brand_with_synthetic_logo(brand_yaml, tmp_path)
    custom.visual_identity.logo_treatment = LogoTreatment.PLAIN
    canvas = Image.new("RGBA", (1080, 1080), (0, 0, 0, 255))
    out, meta = stamp_logo(canvas, custom)

    assert meta["treatment"] == "plain"
    assert meta["position"] is not None
    assert meta["size_px"] is not None
