"""Pure-Pillow creative composition.

Every layout/typography/sizing knob is driven by ``BrandGuidelines`` +
``LayoutTemplate``. Module-level constants exist only as last-resort fallbacks.

This module owns the *implementation* details (overlay rendering, accent
shapes, font auto-shrink, collision-aware disclaimer placement, logo-badge
treatment, output box accounting). The *style* knobs themselves all live in
brand and brief YAML.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from creative_pipeline.tools import contrast as _contrast
from creative_pipeline.schemas import (
    AccentColorRole,
    AccentStyle,
    BrandGuidelines,
    DisclaimerBackgroundTreatment,
    DisclaimerPlacement,
    HeadlineBackgroundTreatment,
    HeadlineCase,
    LayoutTemplate,
    LogoPlacement,
    LogoTreatment,
    OverlayStyle,
    PerAspectLayout,
    ReadabilityFallback,
    TextAlign,
)

logger = logging.getLogger(__name__)

_FALLBACKS = {
    "headline_size_ratio": 0.07,
    "body_size_ratio": 0.022,
    "logo_height_pct": 0.10,
    "luminance_threshold": 140,
    "fonts_dir": "fonts",
    "min_font_size": 16,
}

# ----- helpers --------------------------------------------------------------


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _apply_case(text: str, case: HeadlineCase) -> str:
    if case == HeadlineCase.UPPER:
        return text.upper()
    if case == HeadlineCase.TITLE:
        return text.title()
    if case == HeadlineCase.SENTENCE:
        return text[:1].upper() + text[1:] if text else text
    return text


def _load_font(fonts_dir: str, font_filename: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = Path(fonts_dir) / font_filename
    if not path.exists():
        logger.warning("Font %s not found in %s, falling back to PIL default", font_filename, fonts_dir)
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


def _opposite_panel_color(text_rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    """Pick black or white for the readability panel — whichever contrasts
    better against the rendered text color. Light text → dark panel; dark
    text → light panel. Threshold = 384 (avg 128/channel)."""
    return (0, 0, 0) if sum(text_rgb) > 384 else (255, 255, 255)


def _max_line_width(draw: ImageDraw.ImageDraw, lines: list[str], font) -> int:
    max_w = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        max_w = max(max_w, bbox[2] - bbox[0])
    return max_w


def _line_aligned_panel_x(
    box_x0: int,
    box_x1: int,
    max_line_w: int,
    align: TextAlign,
) -> tuple[int, int]:
    """Panel x-extent matched to the rendered text alignment within the
    headline box (pre-padding)."""
    if align == TextAlign.LEFT:
        return box_x0, box_x0 + max_line_w
    if align == TextAlign.RIGHT:
        return box_x1 - max_line_w, box_x1
    midpoint = (box_x0 + box_x1) // 2
    return midpoint - max_line_w // 2, midpoint + max_line_w // 2


def _render_translucent_panel(
    canvas: Image.Image,
    bbox: tuple[int, int, int, int],
    color_rgb: tuple[int, int, int],
    opacity_pct: float,
    corner_radius_px: int,
) -> Image.Image:
    """Translucent rounded rectangle composited onto the canvas. Used as the
    soft_panel behind the headline and the soft_badge behind the disclaimer."""
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(
        bbox, radius=corner_radius_px,
        fill=color_rgb + (int(opacity_pct * 255),),
    )
    return Image.alpha_composite(canvas, overlay)


_DEFAULT_BUSY_PENALTY = 0.35
_DEFAULT_EDGE_PENALTY = 0.45
_DEFAULT_PRECHECK_RATIO = 3.0
# Normalization caps for scoring. Anything beyond saturates to 1.0.
_TEXTURE_NORM_CAP = 80.0   # grayscale stddev; clean sky ~5, busy garnish 50+
_EDGE_NORM_CAP = 0.15      # Canny edge fraction; clean sky <0.02, busy 0.10+
# Coarse grid for the focal-area estimator. 16×16 is enough for a 1080-tall
# photo and keeps the cost under ~5 ms per frame.
_FOCAL_GRID = 16


def _estimate_focal_area(canvas: Image.Image) -> tuple[tuple[float, float, float, float] | None, float]:
    """Estimate the bounding box of the dominant high-detail region using a
    coarse grid of Canny edge density.

    Generic heuristic: divide the canvas into ``_FOCAL_GRID × _FOCAL_GRID``
    cells, threshold cells whose edge density exceeds ``mean + 0.5σ``, and
    return the bbox covering all over-threshold cells (in unit fractions).
    Returns ``(None, 0.0)`` for very flat photos where no cells stand out.
    The second value is the mean edge density of the cells inside the bbox
    (a rough "focal density" score, not currently used by the scorer).
    """
    import cv2
    import numpy as np

    W, H = canvas.size
    arr = np.asarray(canvas.convert("L"), dtype=np.uint8)
    edges = cv2.Canny(arr, 80, 160)

    cell_h = H // _FOCAL_GRID
    cell_w = W // _FOCAL_GRID
    if cell_h == 0 or cell_w == 0:
        return None, 0.0
    cells = np.zeros((_FOCAL_GRID, _FOCAL_GRID), dtype=np.float32)
    for r in range(_FOCAL_GRID):
        for c in range(_FOCAL_GRID):
            patch = edges[r * cell_h:(r + 1) * cell_h, c * cell_w:(c + 1) * cell_w]
            cells[r, c] = float((patch > 0).mean())

    threshold = float(cells.mean() + 0.5 * cells.std())
    threshold = max(threshold, 0.05)
    mask = (cells >= threshold).astype(np.uint8)
    if not mask.any():
        return None, 0.0

    # Take the *largest connected component* of the dense-cell mask, not the
    # union of every dense cell. On high-edge photos (foliage, water,
    # garnish) the union typically spans the whole frame; the largest
    # component zeroes in on the actual product cluster.
    n_components, labels = cv2.connectedComponents(mask, connectivity=8)
    if n_components <= 1:
        return None, 0.0
    sizes = [int((labels == i).sum()) for i in range(1, n_components)]
    largest_label = 1 + sizes.index(max(sizes))
    component = (labels == largest_label)

    rows = np.where(component.any(axis=1))[0]
    cols = np.where(component.any(axis=0))[0]
    r0, r1 = int(rows.min()), int(rows.max()) + 1
    c0, c1 = int(cols.min()), int(cols.max()) + 1

    box_pct = (
        round(c0 / _FOCAL_GRID, 4),
        round(r0 / _FOCAL_GRID, 4),
        round(c1 / _FOCAL_GRID, 4),
        round(r1 / _FOCAL_GRID, 4),
    )
    focal_density = float(cells[r0:r1, c0:c1][component[r0:r1, c0:c1]].mean())
    return box_pct, round(focal_density, 4)


def _expand_box_pct(
    box_pct: tuple[float, float, float, float],
    pad: float,
) -> tuple[float, float, float, float]:
    """Expand a unit-square bbox by ``pad`` on each side, clamped to [0, 1]."""
    x0, y0, x1, y1 = box_pct
    return (
        max(0.0, x0 - pad),
        max(0.0, y0 - pad),
        min(1.0, x1 + pad),
        min(1.0, y1 + pad),
    )


def _box_gap_px(
    a_pct: tuple[float, float, float, float],
    b_pct: tuple[float, float, float, float],
    canvas_w: int,
    canvas_h: int,
) -> float:
    """Pixel gap between two unit-square boxes. ``0.0`` when they overlap.
    Otherwise the Euclidean distance between the closest corners/edges."""
    ax0, ay0, ax1, ay1 = a_pct
    bx0, by0, bx1, by1 = b_pct
    # Convert to pixel coords on the canvas.
    a_px = (ax0 * canvas_w, ay0 * canvas_h, ax1 * canvas_w, ay1 * canvas_h)
    b_px = (bx0 * canvas_w, by0 * canvas_h, bx1 * canvas_w, by1 * canvas_h)
    # Horizontal / vertical separations (0 when intervals overlap).
    dx = max(0.0, max(b_px[0] - a_px[2], a_px[0] - b_px[2]))
    dy = max(0.0, max(b_px[1] - a_px[3], a_px[1] - b_px[3]))
    return float((dx * dx + dy * dy) ** 0.5)


def _expand_safe_zone_with_clearance(
    focal_pct: tuple[float, float, float, float],
    layout: LayoutTemplate,
    ratio_label: str,
    canvas_w: int,
    canvas_h: int,
) -> tuple[float, float, float, float]:
    """Build the expanded product-safe zone: focal + max(focal_area_padding,
    object_text_clearance_pct, per-aspect min_text_object_gap_px). The
    composer treats this expanded box as the breathing-room border around
    the product."""
    base_pad_pct = layout.focal_area_padding_pct
    clearance_pct = layout.object_text_clearance_pct
    pad_min = min(canvas_w, canvas_h)
    px_min_gap = layout.min_text_object_gap_px.get(ratio_label, 0)
    # Convert px to pct of min canvas dim, then take the max margin.
    px_min_gap_pct = px_min_gap / max(1, pad_min)
    margin_pct = max(base_pad_pct, clearance_pct, px_min_gap_pct)
    return _expand_box_pct(focal_pct, margin_pct)


def _try_shift_box_away_from_focal(
    box_pct: tuple[float, float, float, float],
    focal_safe_zone_pct: tuple[float, float, float, float],
    expanded_safe_zone_pct: tuple[float, float, float, float],
    canvas_w: int,
    canvas_h: int,
    min_gap_px: int,
    min_box_dim_pct: float = 0.18,
) -> tuple[tuple[float, float, float, float] | None, str | None]:
    """If ``box_pct`` overlaps the expanded safe zone, attempt a single
    shrink/shift to clear it. Returns ``(adjusted_box, reason)`` or
    ``(None, reason)`` when no safe shift exists.

    Only horizontal-shrink is attempted (the most common case is
    "candidate's right edge runs into product on the right"). The function
    returns the original box unchanged with reason="no_shift_needed" when
    there's no overlap with the expanded zone in the first place.
    """
    overlap_with_expanded = _box_overlap_pct(box_pct, expanded_safe_zone_pct)
    if overlap_with_expanded == 0:
        return box_pct, "no_shift_needed"

    bx0, by0, bx1, by1 = box_pct
    fx0, fy0, fx1, fy1 = expanded_safe_zone_pct

    # Case 1: box is to the LEFT of the focal zone → shrink x1 leftward.
    if bx0 < fx0 < bx1:
        new_x1_pct = max(bx0 + min_box_dim_pct, fx0 - (min_gap_px / max(1, canvas_w)))
        if new_x1_pct - bx0 >= min_box_dim_pct:
            adjusted = (bx0, by0, new_x1_pct, by1)
            new_overlap = _box_overlap_pct(adjusted, expanded_safe_zone_pct)
            if new_overlap < overlap_with_expanded:
                return (
                    adjusted,
                    f"shrunk x1 {bx1:.3f}→{new_x1_pct:.3f} for {min_gap_px}px clearance",
                )

    # Case 2: box is ABOVE focal → shrink y1 upward.
    if by0 < fy0 < by1:
        new_y1_pct = max(by0 + min_box_dim_pct, fy0 - (min_gap_px / max(1, canvas_h)))
        if new_y1_pct - by0 >= min_box_dim_pct:
            adjusted = (bx0, by0, bx1, new_y1_pct)
            new_overlap = _box_overlap_pct(adjusted, expanded_safe_zone_pct)
            if new_overlap < overlap_with_expanded:
                return (
                    adjusted,
                    f"shrunk y1 {by1:.3f}→{new_y1_pct:.3f} for {min_gap_px}px clearance",
                )

    # Case 3: box is BELOW focal → push y0 downward (raise the top).
    if by0 < fy1 < by1:
        new_y0_pct = min(by1 - min_box_dim_pct, fy1 + (min_gap_px / max(1, canvas_h)))
        if by1 - new_y0_pct >= min_box_dim_pct:
            adjusted = (bx0, new_y0_pct, bx1, by1)
            new_overlap = _box_overlap_pct(adjusted, expanded_safe_zone_pct)
            if new_overlap < overlap_with_expanded:
                return (
                    adjusted,
                    f"raised y0 {by0:.3f}→{new_y0_pct:.3f} for {min_gap_px}px clearance",
                )

    return None, "no_safe_shift_available"


def _widen_box_toward_focal_safe_zone(
    box_pct: tuple[float, float, float, float],
    focal_safe_zone_pct: tuple[float, float, float, float] | None,
    min_gap_px: int,
    canvas_w: int,
    canvas_h: int,
    max_widen_pct: float,
    epsilon_px: float = 1.0,
) -> tuple[tuple[float, float, float, float], str, float]:
    """Extend ``box_pct`` toward the focal safe zone, up to a clearance
    of ``min_gap_px``. Caps the absolute widen to
    ``max_widen_pct * canvas_w``.

    Returns ``(new_box, reason, width_delta_pct)``. When no widening is
    safe or beneficial, returns the original box and a reason string.
    Always-on but bounded by ``max_widen_pct=0`` (disabled).
    """
    if max_widen_pct <= 0.0:
        return box_pct, "disabled", 0.0
    if focal_safe_zone_pct is None:
        return box_pct, "no_focal_safe_zone", 0.0

    bx0, by0, bx1, by1 = box_pct
    fx0, fy0, fx1, fy1 = focal_safe_zone_pct
    # Direction: focal to the RIGHT of box → can widen x_max rightward.
    # Direction: focal to the LEFT of box → can widen x_min leftward.
    bx_center = (bx0 + bx1) / 2
    fx_center = (fx0 + fx1) / 2

    cap_pct = max_widen_pct  # in fractions of canvas width
    epsilon_pct = epsilon_px / max(1, canvas_w)
    gap_pct = min_gap_px / max(1, canvas_w)

    if fx_center >= bx_center:
        # Focal is on the right → push x_max right, but stop before
        # entering the safe zone (minus the required gap).
        max_safe_x = fx0 - gap_pct - epsilon_pct
        if bx1 + epsilon_pct >= max_safe_x:
            return box_pct, "already_wide_enough", 0.0
        widen_to = min(max_safe_x, bx1 + cap_pct)
        if widen_to <= bx1 + epsilon_pct:
            return box_pct, "no_widen_room", 0.0
        delta = widen_to - bx1
        return (bx0, by0, widen_to, by1), (
            f"widened x_max {bx1:.3f}→{widen_to:.3f} (+{delta:.3f}) "
            f"toward focal at x={fx0:.3f}"
        ), delta
    else:
        # Focal is on the left → push x_min leftward.
        min_safe_x = fx1 + gap_pct + epsilon_pct
        if bx0 - epsilon_pct <= min_safe_x:
            return box_pct, "already_wide_enough", 0.0
        widen_to = max(min_safe_x, bx0 - cap_pct)
        if widen_to >= bx0 - epsilon_pct:
            return box_pct, "no_widen_room", 0.0
        delta = bx0 - widen_to
        return (widen_to, by0, bx1, by1), (
            f"widened x_min {bx0:.3f}→{widen_to:.3f} (-{delta:.3f}) "
            f"toward focal at x={fx1:.3f}"
        ), delta


def _build_shift_candidates(
    box_pct: tuple[float, float, float, float],
    focal_safe_zone_pct: tuple[float, float, float, float],
    canvas_w: int,
    canvas_h: int,
    min_gap_px: int,
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Deterministic list of shift candidates (name, shifted_box). Direction
    chosen from the focal area's centroid relative to the box centroid;
    always includes narrowing the side facing the focal object as a
    last-resort attempt that preserves the box origin."""
    canvas_min = max(1, min(canvas_w, canvas_h))
    step = max(min_gap_px / canvas_min, 0.025)
    bx_c = (box_pct[0] + box_pct[2]) / 2
    by_c = (box_pct[1] + box_pct[3]) / 2
    fx_c = (focal_safe_zone_pct[0] + focal_safe_zone_pct[2]) / 2
    fy_c = (focal_safe_zone_pct[1] + focal_safe_zone_pct[3]) / 2

    raw: list[tuple[str, tuple[float, float, float, float] | None]] = []
    if fx_c >= bx_c:
        raw.append(("shift_left_1x", _shift_box_pct(box_pct, dx=-step)))
        raw.append(("shift_left_2x", _shift_box_pct(box_pct, dx=-2 * step)))
        raw.append(("narrow_right_1x", _shift_box_pct(box_pct, shrink_right=step)))
        raw.append(("narrow_right_2x", _shift_box_pct(box_pct, shrink_right=2 * step)))
    else:
        raw.append(("shift_right_1x", _shift_box_pct(box_pct, dx=step)))
        raw.append(("narrow_left_1x", _shift_box_pct(box_pct, shrink_left=step)))
    if fy_c >= by_c:
        raw.append(("shift_up_1x", _shift_box_pct(box_pct, dy=-step)))
        raw.append(("shift_up_2x", _shift_box_pct(box_pct, dy=-2 * step)))
    else:
        raw.append(("shift_down_1x", _shift_box_pct(box_pct, dy=step)))
        raw.append(("shift_down_2x", _shift_box_pct(box_pct, dy=2 * step)))
    return [(name, sb) for name, sb in raw if sb is not None]


def _shift_box_pct(
    box_pct: tuple[float, float, float, float],
    dx: float = 0.0,
    dy: float = 0.0,
    shrink_right: float = 0.0,
    shrink_left: float = 0.0,
) -> tuple[float, float, float, float] | None:
    """Translate / shrink a unit-square box. Returns ``None`` when the
    result would leave the unit square or invert."""
    x0, y0, x1, y1 = box_pct
    nx0 = x0 + dx + shrink_left
    nx1 = x1 + dx - shrink_right
    ny0 = y0 + dy
    ny1 = y1 + dy
    # Keep inside [0, 1] and a sensible minimum width.
    if nx0 < 0.0 or ny0 < 0.0 or nx1 > 1.0 or ny1 > 1.0:
        return None
    if nx1 - nx0 < 0.16 or ny1 - ny0 < 0.10:
        return None
    return (nx0, ny0, nx1, ny1)


def _box_overlap_pct(
    a_pct: tuple[float, float, float, float],
    b_pct: tuple[float, float, float, float],
) -> float:
    """Fraction of ``a_pct``'s area that lies inside ``b_pct``."""
    ax0, ay0, ax1, ay1 = a_pct
    bx0, by0, bx1, by1 = b_pct
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    a_area = max(1e-9, (ax1 - ax0) * (ay1 - ay0))
    return float(inter / a_area)


def _rendered_text_bbox_px(
    box_px: tuple[int, int, int, int],
    max_line_w: int,
    total_text_h: int,
    text_align: "TextAlign",
) -> tuple[int, int, int, int]:
    """Pixel bbox of the actual rendered text inside its candidate box —
    accounts for line wrapping width (≤ box width) and alignment.

    This is the bbox the composer uses for object-clearance checks,
    contrast sampling, and prominence scoring (instead of the candidate
    box, which can be wider than the text)."""
    x0, y0, x1, _ = box_px
    if text_align == TextAlign.LEFT:
        rx0, rx1 = x0, x0 + max_line_w
    elif text_align == TextAlign.RIGHT:
        rx0, rx1 = x1 - max_line_w, x1
    else:  # CENTER
        mid = (x0 + x1) // 2
        rx0 = mid - max_line_w // 2
        rx1 = mid + max_line_w // 2
    return (rx0, y0, rx1, y0 + total_text_h)


def _bbox_to_pct(
    box_px: tuple[int, int, int, int], canvas_w: int, canvas_h: int,
) -> tuple[float, float, float, float]:
    return (
        box_px[0] / max(1, canvas_w),
        box_px[1] / max(1, canvas_h),
        box_px[2] / max(1, canvas_w),
        box_px[3] / max(1, canvas_h),
    )


def _compute_composition_score(
    crop_clearance_pass: bool,
    crop_clip_detected: bool,
    logo_clearance_pass: bool,
    logo_collision: bool,
    headline_prominence: float,
    headline_clearance_pass: bool,
    headline_too_small: bool,
    accent_safe_zone_pass: bool,
    disclaimer_clearance_pass: bool,
    all_text_candidates_failed: bool,
) -> dict:
    """Final composition score aggregating crop / logo / headline /
    accent / disclaimer health into one number in [0, 1] plus a
    machine-readable warning list. The score weights:
      - crop_clearance     0.18
      - logo_clearance     0.15
      - headline_prominence 0.27 (the existing prominence factor)
      - headline_clearance 0.15
      - accent_safe_zone   0.10
      - disclaimer_clearance 0.15
    """
    crop_factor = 1.0 if crop_clearance_pass else (0.3 if crop_clip_detected else 0.7)
    logo_factor = 1.0 if logo_clearance_pass else (0.3 if logo_collision else 0.6)
    headline_clear_factor = 1.0 if headline_clearance_pass else 0.4
    headline_size_factor = 0.5 if headline_too_small else 1.0
    accent_factor = 1.0 if accent_safe_zone_pass else 0.7
    disclaimer_factor = 1.0 if disclaimer_clearance_pass else 0.6

    # Bound prominence into the same [0, 1] band as the other factors.
    # Prominence already mixes its own weights — we just use it directly
    # as the headline contribution and apply the size_factor as a
    # multiplier so a "too small" floor caps the head room.
    prominence = max(0.0, min(1.0, headline_prominence)) * headline_size_factor

    score = (
        0.18 * crop_factor
        + 0.15 * logo_factor
        + 0.27 * prominence
        + 0.15 * headline_clear_factor
        + 0.10 * accent_factor
        + 0.15 * disclaimer_factor
    )

    warnings: list[str] = []
    if crop_clip_detected:
        warnings.append("focal_edge_clip")
    elif not crop_clearance_pass:
        warnings.append("focal_near_canvas_edge")
    if logo_collision:
        warnings.append("logo_product_collision")
    elif not logo_clearance_pass:
        warnings.append("logo_near_product_edge")
    if headline_too_small:
        warnings.append("headline_too_small")
    if not headline_clearance_pass:
        warnings.append("headline_near_object")
    if not accent_safe_zone_pass:
        warnings.append("accent_hugs_edge")
    if not disclaimer_clearance_pass:
        warnings.append("disclaimer_near_object")
    if all_text_candidates_failed:
        warnings.append("all_candidates_failed_clearance")

    return {
        "composition_score": round(score, 3),
        "composition_warnings": warnings,
        "composition_factors": {
            "crop": round(crop_factor, 3),
            "logo": round(logo_factor, 3),
            "headline_prominence": round(prominence, 3),
            "headline_clearance": round(headline_clear_factor, 3),
            "headline_size": round(headline_size_factor, 3),
            "accent": round(accent_factor, 3),
            "disclaimer": round(disclaimer_factor, 3),
        },
    }


def _compute_prominence_score(
    font_size_px: int,
    max_size_px: int,
    min_size_px: int,
    zone_fill_pct: float,
    target_zone_fill_pct: float,
    line_count: int,
    preferred_lines: int | None,
    min_lines: int | None,
    max_lines: int | None,
    fit_status: str,
    contrast: float,
    clearance_pass: bool,
    collision: bool,
    near_miss: bool,
) -> dict:
    """Weighted prominence score in [0, 1] indicating how confidently the
    headline reads as campaign hero copy. Components are returned alongside
    the aggregate so the report can show which factor dominates a low score.

    Weights (sum 1.0):
      - size_factor          0.30 — chosen size relative to ceiling
      - zone_fill_factor     0.20 — vertical fill of the headline box
      - line_factor          0.10 — preferred / in-bounds line count
      - fit_factor           0.10 — type fits target without overflow
      - contrast_factor      0.10 — local contrast ≥ AA
      - clearance_factor     0.20 — distance from the focal/product object
    """
    max_size_px = max(1, max_size_px)
    size_factor = min(1.0, font_size_px / max_size_px)

    # Zone fill: 1.0 at the target, gracefully drops below; modest reward
    # for exceeding (capped) so 0.85 fills don't beat 0.62-target = 1.0.
    target = max(0.05, target_zone_fill_pct)
    zone_fill_factor = min(1.0, zone_fill_pct / target)

    if preferred_lines is not None and line_count == preferred_lines:
        line_factor = 1.0
    elif (
        (min_lines is None or line_count >= min_lines)
        and (max_lines is None or line_count <= max_lines)
    ):
        line_factor = 0.85
    else:
        line_factor = 0.5

    fit_factor = {"fits": 1.0, "near_fit": 0.85, "overflow": 0.4}.get(fit_status, 0.6)

    # AA = 4.5; AAA = 7.0+. 7+ saturates at 1.0; 4.5 = 0.64; 3.0 = 0.43.
    contrast_factor = min(1.0, max(0.0, contrast / 7.0))

    if collision:
        clearance_factor = 0.2
    elif near_miss:
        clearance_factor = 0.55
    else:
        clearance_factor = 1.0 if clearance_pass else 0.7

    score = (
        0.30 * size_factor
        + 0.20 * zone_fill_factor
        + 0.10 * line_factor
        + 0.10 * fit_factor
        + 0.10 * contrast_factor
        + 0.20 * clearance_factor
    )
    return {
        "headline_prominence_score": round(score, 3),
        "headline_size_factor": round(size_factor, 3),
        "headline_zone_fill_factor": round(zone_fill_factor, 3),
        "headline_line_factor": round(line_factor, 3),
        "headline_fit_factor": round(fit_factor, 3),
        "headline_contrast_factor": round(contrast_factor, 3),
        "headline_clearance_factor": round(clearance_factor, 3),
        "headline_zone_fill_pct": round(zone_fill_pct, 3),
    }


def _clearance_metrics(
    rendered_bbox_pct: tuple[float, float, float, float],
    focal_safe_zone_pct: tuple[float, float, float, float] | None,
    canvas_w: int,
    canvas_h: int,
    min_gap_px: int,
) -> dict:
    """Return collision/near-miss/gap metrics for a rendered text bbox vs.
    the unexpanded focal safe zone."""
    if focal_safe_zone_pct is None:
        return {
            "gap_px": None,
            "collision": False,
            "near_miss": False,
            "clearance_pass": True,
            "overlap_pct": 0.0,
        }
    overlap = _box_overlap_pct(rendered_bbox_pct, focal_safe_zone_pct)
    gap = _box_gap_px(rendered_bbox_pct, focal_safe_zone_pct, canvas_w, canvas_h)
    collision = overlap > 0.0
    near_miss = (not collision) and (min_gap_px > 0) and (gap < min_gap_px)
    return {
        "gap_px": round(gap, 1),
        "collision": collision,
        "near_miss": near_miss,
        "clearance_pass": (not collision) and (not near_miss),
        "overlap_pct": round(overlap, 4),
    }


def _region_texture_score(canvas: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Grayscale standard deviation inside ``box``. Higher = busier photography."""
    import numpy as np
    arr = np.asarray(canvas.crop(box).convert("L"), dtype=np.float32)
    return float(arr.std())


def _region_edge_density(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    canny_low: int = 80,
    canny_high: int = 160,
) -> float:
    """Fraction of pixels classified as edges by cv2.Canny inside ``box``.
    Returns a value in [0, 1]. High-detail object clusters (lime slices,
    mint leaves, product edges) push this up; clean sky/water/sand stays
    well below 0.05."""
    import cv2
    import numpy as np
    arr = np.asarray(canvas.crop(box).convert("L"), dtype=np.uint8)
    if arr.size == 0:
        return 0.0
    edges = cv2.Canny(arr, canny_low, canny_high)
    return float((edges > 0).sum()) / float(edges.size)


def _score_candidate_box(
    canvas: Image.Image,
    box_pct: tuple[float, float, float, float],
    target_w: int,
    target_h: int,
    text_color_options: list[str],
    busy_w: float,
    edge_w: float,
    min_precheck: float,
    focal_safe_zone_pct: tuple[float, float, float, float] | None = None,
    expanded_safe_zone_pct: tuple[float, float, float, float] | None = None,
    focal_overlap_w: float = 0.0,
    clearance_penalty: float = 0.0,
    min_gap_px: int = 0,
    hard_fail_collision: bool = False,
    hard_fail_near_miss: bool = False,
    max_texture_norm: float = 1.0,
    max_edge_norm: float = 1.0,
) -> dict:
    """Score a single candidate headline zone. Returns a dict with all
    metrics so callers (and the report) can audit the decision.

    A candidate is marked ``viable=False`` when:
      - its best-color contrast is below ``min_precheck``, OR
      - its normalized texture exceeds ``max_texture_norm``, OR
      - its normalized edge density exceeds ``max_edge_norm``.

    Non-viable candidates receive a -1.0 score penalty so a viable
    alternative is always preferred when one exists. ``focal_overlap_pct``
    further penalizes candidates that intersect ``focal_safe_zone_pct``.
    """
    box_px = (
        int(box_pct[0] * target_w),
        int(box_pct[1] * target_h),
        int(box_pct[2] * target_w),
        int(box_pct[3] * target_h),
    )
    bg = _box_mean_rgb_ext(canvas, box_px)
    contrasts = [
        (color_hex, _contrast.contrast_ratio(_contrast.hex_to_rgb(color_hex), bg))
        for color_hex in text_color_options
    ]
    contrasts.sort(key=lambda x: x[1], reverse=True)
    best_color, best_contrast = contrasts[0]

    texture = _region_texture_score(canvas, box_px)
    edge_density = _region_edge_density(canvas, box_px)

    contrast_norm = min(best_contrast, 21.0) / 21.0
    texture_norm = min(texture, _TEXTURE_NORM_CAP) / _TEXTURE_NORM_CAP
    edge_norm = min(edge_density, _EDGE_NORM_CAP) / _EDGE_NORM_CAP

    focal_overlap_pct = (
        _box_overlap_pct(box_pct, focal_safe_zone_pct)
        if focal_safe_zone_pct is not None else 0.0
    )

    # Object-clearance: distance to the unexpanded focal safe zone (the
    # actual product area), and overlap with the expanded zone (focal +
    # breathing room). Hard-fail when overlapping the unexpanded zone.
    text_object_gap_px = float("inf")
    expanded_overlap_pct = 0.0
    near_miss = False
    hard_collision = False
    if focal_safe_zone_pct is not None:
        text_object_gap_px = _box_gap_px(box_pct, focal_safe_zone_pct, target_w, target_h)
        if expanded_safe_zone_pct is not None:
            expanded_overlap_pct = _box_overlap_pct(box_pct, expanded_safe_zone_pct)
        if focal_overlap_pct > 0:
            hard_collision = True
        elif min_gap_px > 0 and text_object_gap_px < min_gap_px:
            near_miss = True

    score = (
        contrast_norm
        - busy_w * texture_norm
        - edge_w * edge_norm
        - focal_overlap_w * focal_overlap_pct
    )
    if expanded_overlap_pct > 0 and not hard_collision:
        score -= clearance_penalty * expanded_overlap_pct
    if near_miss:
        # Soft penalty even when boxes don't overlap the expanded zone
        # but sit within the configured pixel gap.
        score -= clearance_penalty * 0.5

    contrast_ok = best_contrast >= min_precheck
    busy_ok = texture_norm <= max_texture_norm
    edge_ok = edge_norm <= max_edge_norm
    collision_ok = (not hard_collision) if hard_fail_collision else True
    near_miss_ok = (not near_miss) if hard_fail_near_miss else True
    clearance_ok = collision_ok and near_miss_ok
    viable = contrast_ok and busy_ok and edge_ok and clearance_ok
    if not viable:
        score -= 1.0  # ranks any viable alternative above this one

    rejection_reasons: list[str] = []
    if not contrast_ok:
        rejection_reasons.append(f"contrast {best_contrast:.2f} < {min_precheck}")
    if not busy_ok:
        rejection_reasons.append(f"texture_norm {texture_norm:.2f} > {max_texture_norm}")
    if not edge_ok:
        rejection_reasons.append(f"edge_norm {edge_norm:.2f} > {max_edge_norm}")
    if hard_collision and hard_fail_collision:
        rejection_reasons.append(
            f"hard_collision (overlap_pct={focal_overlap_pct:.3f} with focal safe zone)"
        )
    if near_miss and hard_fail_near_miss:
        rejection_reasons.append(
            f"near_miss (gap_px={text_object_gap_px:.1f} < {min_gap_px})"
        )

    return {
        "box_pct": [round(c, 4) for c in box_pct],
        "box_px": list(box_px),
        "best_text_color": best_color,
        "contrast_estimate": round(best_contrast, 2),
        "texture_score": round(texture, 2),
        "texture_norm": round(texture_norm, 4),
        "edge_density": round(edge_density, 4),
        "edge_norm": round(edge_norm, 4),
        "focal_overlap_pct": round(focal_overlap_pct, 4),
        "expanded_overlap_pct": round(expanded_overlap_pct, 4),
        "text_object_gap_px": (
            round(text_object_gap_px, 1) if text_object_gap_px != float("inf") else None
        ),
        "near_miss": near_miss,
        "hard_collision": hard_collision,
        "contrast_ok": contrast_ok,
        "clearance_ok": clearance_ok,
        "viable": viable,
        "rejection_reasons": rejection_reasons,
        "score": round(score, 4),
    }


def _select_headline_box(
    cropped_hero: Image.Image,
    layout: LayoutTemplate,
    per_aspect: PerAspectLayout,
    brand: BrandGuidelines,
    target_w: int,
    target_h: int,
) -> tuple[tuple[float, float, float, float], dict, list[dict], dict]:
    """Pick the cleanest configured headline zone, or fall back to the
    single ``per_aspect.headline_box`` when no candidates are configured.

    Returns:
        - selected_box_pct (x0, y0, x1, y1) fractions
        - selection_meta dict (reason, score, metrics for the *winner*)
        - all_candidate_scores list (every candidate's metrics, for audit)
        - focal_meta dict with focal_area_estimate, product_safe_zone_box,
          focal_overlap_detected
    """
    candidates = list(per_aspect.headline_candidate_boxes)
    scoring_on = (
        layout.enable_candidate_headline_scoring
        and per_aspect.avoid_busy_regions
        and bool(candidates)
    )

    # Always estimate focal area when avoid_focal_overlap is on, even if
    # we're falling back to the single headline_box — the report still
    # benefits from the audit fields.
    focal_pct = focal_density = None
    safe_zone_pct = None
    expanded_safe_zone_pct = None
    if layout.avoid_focal_overlap:
        focal_pct, focal_density = _estimate_focal_area(cropped_hero)
        if focal_pct is not None:
            safe_zone_pct = _expand_box_pct(focal_pct, layout.focal_area_padding_pct)
            # Build the *expanded* safe zone using max of focal_area_padding,
            # object_text_clearance_pct, and per-aspect min_text_object_gap_px.
            ratio_label = next(
                (k for k, v in brand.aspect_ratios.items()
                 if v[0] == target_w and v[1] == target_h),
                "",
            )
            expanded_safe_zone_pct = _expand_safe_zone_with_clearance(
                focal_pct, layout, ratio_label, target_w, target_h,
            )
    min_gap_px = 0
    if expanded_safe_zone_pct is not None:
        ratio_label = next(
            (k for k, v in brand.aspect_ratios.items()
             if v[0] == target_w and v[1] == target_h),
            "",
        )
        min_gap_px = layout.min_text_object_gap_px.get(ratio_label, 0)

    if not scoring_on:
        # Fallback path: the single configured headline_box.
        sel = per_aspect.headline_box
        focal_overlap = (
            _box_overlap_pct(sel, safe_zone_pct)
            if safe_zone_pct is not None else 0.0
        )
        gap_px = (
            _box_gap_px(sel, safe_zone_pct, target_w, target_h)
            if safe_zone_pct is not None else None
        )
        return (
            sel,
            {
                "selection_reason": "single_configured_box (scoring off / no candidates)",
                "score": None,
                "contrast_estimate": None,
                "texture_score": None,
                "edge_density": None,
                "best_text_color": None,
                "headline_box_original": list(sel),
                "headline_box_adjusted": None,
                "headline_box_adjustment_reason": "no_shift_needed",
            },
            [],
            {
                "focal_area_estimate": list(focal_pct) if focal_pct else None,
                "product_safe_zone_box": list(safe_zone_pct) if safe_zone_pct else None,
                "expanded_product_safe_zone_box": (
                    list(expanded_safe_zone_pct) if expanded_safe_zone_pct else None
                ),
                "focal_overlap_detected": focal_overlap > 0.05,
                "focal_overlap_pct": round(focal_overlap, 4),
                "focal_near_miss_detected": (
                    gap_px is not None and gap_px > 0 and gap_px < min_gap_px
                ),
                "text_object_gap_px": round(gap_px, 1) if gap_px is not None else None,
                "text_object_clearance_pass": (
                    True if safe_zone_pct is None
                    else (focal_overlap == 0.0 and (gap_px is None or gap_px >= min_gap_px))
                ),
                "all_candidates_failed_clearance": False,
                "focal_density": focal_density,
                "min_gap_px_threshold": min_gap_px,
            },
        )

    # Evaluate against the post-overlay canvas so contrast estimates match
    # what the rest of the composer will see.
    overlaid = _render_overlay(cropped_hero, layout)
    text_color_options = [
        brand.typography.text_color_on_dark,
        brand.typography.text_color_on_light,
    ]
    busy_w = (
        per_aspect.busy_region_penalty
        if per_aspect.busy_region_penalty is not None
        else _DEFAULT_BUSY_PENALTY
    )
    edge_w = (
        per_aspect.object_overlap_penalty
        if per_aspect.object_overlap_penalty is not None
        else _DEFAULT_EDGE_PENALTY
    )
    precheck = (
        per_aspect.min_contrast_precheck
        if per_aspect.min_contrast_precheck is not None
        else _DEFAULT_PRECHECK_RATIO
    )

    scored = [
        _score_candidate_box(
            overlaid, box_pct, target_w, target_h,
            text_color_options, busy_w, edge_w, precheck,
            focal_safe_zone_pct=safe_zone_pct,
            expanded_safe_zone_pct=expanded_safe_zone_pct,
            focal_overlap_w=layout.focal_overlap_penalty_weight,
            clearance_penalty=layout.object_clearance_penalty,
            min_gap_px=min_gap_px,
            hard_fail_collision=layout.hard_fail_text_object_collision,
            hard_fail_near_miss=layout.hard_fail_text_object_near_miss,
            max_texture_norm=layout.text_region_max_texture_score,
            max_edge_norm=layout.text_region_max_edge_density,
        )
        for box_pct in candidates
    ]
    # Track whether *every* candidate failed clearance — surfaced in the
    # report so the run is auditable when no viable zone existed.
    all_clearance_failed = (
        bool(scored)
        and all(
            (s["hard_collision"] and layout.hard_fail_text_object_collision)
            or (s["near_miss"] and layout.hard_fail_text_object_near_miss)
            for s in scored
        )
    )
    # Highest score wins; preserve original list order in the audit trail.
    winner = max(scored, key=lambda s: s["score"])
    runner_up_gap = None
    if len(scored) > 1:
        runner_up = sorted((s for s in scored if s is not winner),
                            key=lambda s: s["score"], reverse=True)[0]
        runner_up_gap = round(winner["score"] - runner_up["score"], 4)

    selected_box_pct = (
        winner["box_pct"][0], winner["box_pct"][1],
        winner["box_pct"][2], winner["box_pct"][3],
    )
    headline_box_original = list(selected_box_pct)
    headline_box_adjusted: list[float] | None = None
    adjustment_reason = "no_shift_needed"

    # If the winner has near-miss with focal area, attempt a single shift to
    # widen the gap. Only fires when an expanded safe zone exists.
    if (
        expanded_safe_zone_pct is not None
        and (winner["near_miss"] or winner["expanded_overlap_pct"] > 0)
        and not winner["hard_collision"]
    ):
        adjusted, reason = _try_shift_box_away_from_focal(
            selected_box_pct,
            safe_zone_pct,  # unexpanded — what we must clear
            expanded_safe_zone_pct,
            target_w,
            target_h,
            min_gap_px=max(min_gap_px, 1),
        )
        if adjusted is not None and adjusted != selected_box_pct:
            selected_box_pct = adjusted
            headline_box_adjusted = list(adjusted)
            adjustment_reason = reason or "shifted"
        elif reason:
            adjustment_reason = reason

    reason_bits = [
        f"score={winner['score']}",
        f"contrast≈{winner['contrast_estimate']}",
        f"texture={winner['texture_score']}",
        f"edges={winner['edge_density']}",
        f"focal_overlap={winner['focal_overlap_pct']}",
        f"gap_px={winner['text_object_gap_px']}",
        f"viable={winner['viable']}",
    ]
    if runner_up_gap is not None:
        reason_bits.append(f"runner-up gap={runner_up_gap}")
    reason = "best of " + str(len(scored)) + " candidates: " + ", ".join(reason_bits)

    sel_focal_overlap = winner["focal_overlap_pct"]
    sel_gap_px = winner["text_object_gap_px"]

    return (
        selected_box_pct,
        {
            "selection_reason": reason,
            "score": winner["score"],
            "contrast_estimate": winner["contrast_estimate"],
            "texture_score": winner["texture_score"],
            "edge_density": winner["edge_density"],
            "best_text_color": winner["best_text_color"],
            "viable": winner["viable"],
            "headline_box_original": headline_box_original,
            "headline_box_adjusted": headline_box_adjusted,
            "headline_box_adjustment_reason": adjustment_reason,
        },
        scored,
        {
            "focal_area_estimate": list(focal_pct) if focal_pct else None,
            "product_safe_zone_box": list(safe_zone_pct) if safe_zone_pct else None,
            "expanded_product_safe_zone_box": (
                list(expanded_safe_zone_pct) if expanded_safe_zone_pct else None
            ),
            "focal_overlap_detected": sel_focal_overlap > 0.05,
            "focal_overlap_pct": sel_focal_overlap,
            "focal_near_miss_detected": winner["near_miss"],
            "text_object_gap_px": sel_gap_px,
            "text_object_clearance_pass": (
                winner["clearance_ok"] and not winner["near_miss"]
            ),
            "all_candidates_failed_clearance": all_clearance_failed,
            "focal_density": focal_density,
            "min_gap_px_threshold": min_gap_px,
        },
    )


def _box_mean_rgb_ext(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
    """Box mean RGB without text-color filtering — same shape used by the
    composer's pre-render contrast estimate."""
    crop = image.crop(box).convert("RGB").resize((1, 1), Image.BOX)
    return crop.getpixel((0, 0))


def _estimate_text_contrast(
    canvas: Image.Image,
    text_box: tuple[int, int, int, int],
    text_rgb: tuple[int, int, int],
) -> float:
    bg = _box_mean_rgb_ext(canvas, text_box)
    return _contrast.contrast_ratio(text_rgb, bg)


def _render_feathered_local_gradient(
    canvas: Image.Image,
    text_box: tuple[int, int, int, int],
    text_rgb: tuple[int, int, int],
    opacity_pct: float = 0.30,
    feather_pct: float = 0.025,
) -> Image.Image:
    """Subtle feathered rectangle behind text — much softer than soft_panel.
    Used as a fallback when natural composition isn't quite enough."""
    W, H = canvas.size
    color = _opposite_panel_color(text_rgb)
    pad = max(4, int(min(W, H) * 0.012))
    inner = (
        max(0, text_box[0] - pad),
        max(0, text_box[1] - pad),
        min(W, text_box[2] + pad),
        min(H, text_box[3] + pad),
    )
    alpha_mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(alpha_mask).rectangle(inner, fill=int(opacity_pct * 255))
    alpha_mask = alpha_mask.filter(
        ImageFilter.GaussianBlur(radius=max(2, int(min(W, H) * feather_pct)))
    )
    color_layer = Image.new("RGBA", (W, H), color + (255,))
    color_layer.putalpha(alpha_mask)
    return Image.alpha_composite(canvas, color_layer)


def _apply_readability_fallback(
    canvas: Image.Image,
    text_box: tuple[int, int, int, int],
    headline_box: tuple[int, int, int, int],
    text_align: TextAlign,
    text_rgb: tuple[int, int, int],
    layout: LayoutTemplate,
    brand: BrandGuidelines,
) -> tuple[Image.Image, str, float]:
    """Walk ``layout.readability_fallback_order`` until the contrast estimate
    meets the brand threshold (or the chain is exhausted). Returns the canvas
    with whatever fallback was applied, a label naming the step that ended
    the walk, and the final contrast estimate.

    "Natural composition" (no fallback applied) is reported when the initial
    estimate already meets the threshold. Steps that aren't yet implemented
    (``REPOSITION_WITHIN_TEXT_SAFE_AREA``) are skipped without error.
    """
    qc = brand.qc
    if not brand.required_brand_checks.contrast_ratio:
        # Contrast QC disabled → no fallback.
        return canvas, "natural_composition", _estimate_text_contrast(canvas, text_box, text_rgb)

    box_h = max(1, text_box[3] - text_box[1])
    is_large = box_h >= qc.large_text_size_threshold_px
    threshold = qc.large_text_min_ratio if is_large else qc.min_contrast_ratio

    estimate = _estimate_text_contrast(canvas, text_box, text_rgb)
    if estimate >= threshold:
        return canvas, "natural_composition", estimate

    for step in layout.readability_fallback_order:
        if step == ReadabilityFallback.CHOOSE_BEST_BRAND_TEXT_COLOR:
            # Already done before this fn (in _choose_text_treatment).
            continue
        if step == ReadabilityFallback.SUBTLE_TEXT_SHADOW:
            # Shadow is rendered later by the headline draw call when
            # layout.headline_text_shadow is true; it doesn't change the
            # sampled background. As a fallback step, we recognise that the
            # shadow is in play and reflect a perceptual contrast bump.
            if layout.headline_text_shadow:
                bumped = estimate * 1.15
                if bumped >= threshold:
                    return canvas, "subtle_text_shadow", bumped
                estimate = bumped
            continue
        if step == ReadabilityFallback.REPOSITION_WITHIN_TEXT_SAFE_AREA:
            # Not implemented in the current composer; skip without error.
            continue
        if step == ReadabilityFallback.SUBTLE_LOCAL_GRADIENT:
            canvas = _render_feathered_local_gradient(canvas, text_box, text_rgb, opacity_pct=0.32)
            estimate = _estimate_text_contrast(canvas, text_box, text_rgb)
            if estimate >= threshold:
                return canvas, "subtle_local_gradient", estimate
            continue
        if step == ReadabilityFallback.SOFT_PANEL_LAST_RESORT:
            # Build a panel matched to the rendered text alignment.
            max_text_w = text_box[2] - text_box[0]
            text_x0, text_x1 = _line_aligned_panel_x(
                headline_box[0], headline_box[2], max_text_w, text_align,
            )
            pad_px = max(1, int(min(canvas.size) * layout.headline_panel_padding_pct))
            panel_box = (
                max(0, text_x0 - pad_px),
                max(0, text_box[1] - pad_px),
                min(canvas.size[0], text_x1 + pad_px),
                min(canvas.size[1], text_box[3] + pad_px),
            )
            radius_px = max(0, int(min(canvas.size) * layout.headline_panel_corner_radius_pct))
            panel_color = _opposite_panel_color(text_rgb)
            canvas = _render_translucent_panel(
                canvas, panel_box, panel_color,
                layout.headline_panel_opacity_pct, radius_px,
            )
            estimate = _estimate_text_contrast(canvas, text_box, text_rgb)
            return canvas, "soft_panel_last_resort", estimate

    return canvas, "exhausted_fallbacks", estimate


def _draw_text_with_optional_shadow(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font,
    color: tuple[int, int, int, int],
    shadow: bool,
) -> None:
    if shadow:
        # Subtle drop-shadow scaled to font size; slightly translucent black.
        offset = max(1, int(getattr(font, "size", 24) * 0.04))
        draw.text((pos[0] + offset, pos[1] + offset), text, font=font, fill=(0, 0, 0, 130))
    draw.text(pos, text, font=font, fill=color)


def _resolve_role_color(role: AccentColorRole, brand: BrandGuidelines) -> str:
    return {
        AccentColorRole.PRIMARY: brand.visual_identity.primary_color,
        AccentColorRole.SECONDARY: brand.visual_identity.secondary_color,
        AccentColorRole.ACCENT: brand.visual_identity.accent_color,
    }[role]


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for w in words[1:]:
        candidate = f"{current} {w}"
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    lines.append(current)
    return lines


def _line_height(draw: ImageDraw.ImageDraw, font) -> int:
    bbox = draw.textbbox((0, 0), "Mg", font=font)
    return max(1, bbox[3] - bbox[1])


def _wrap_text_balanced(
    draw: ImageDraw.ImageDraw,
    text: str,
    font,
    max_width: int,
    target_line_count: int,
) -> list[str] | None:
    """Split ``text`` into exactly ``target_line_count`` contiguous-word
    groups minimizing ``max_line_width − min_line_width``. Returns
    ``None`` when no split is achievable (any group's rendered width
    exceeds ``max_width``).

    Used by the fitter as an alternative to greedy wrap when greedy
    produces single-word lines on narrow boxes. Enumerates all
    ``C(n-1, k-1)`` cut positions; with k ≤ 4 and n ≤ ~15 this is
    cheap (≤ a few hundred candidates per font size)."""
    words = text.split()
    n = len(words)
    if target_line_count < 1 or target_line_count > n:
        return None
    if target_line_count == 1:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] > max_width:
            return None
        return [text]

    def _measure(group: list[str]) -> int:
        joined = " ".join(group)
        bbox = draw.textbbox((0, 0), joined, font=font)
        return bbox[2] - bbox[0]

    # Enumerate every way to place (target_line_count - 1) cuts in the
    # n-1 inter-word slots. Keep only splits whose every line fits.
    from itertools import combinations
    best_split: tuple[list[str], ...] | None = None
    best_spread = float("inf")
    for cuts in combinations(range(1, n), target_line_count - 1):
        groups = []
        prev = 0
        for c in cuts:
            groups.append(words[prev:c])
            prev = c
        groups.append(words[prev:])
        widths = [_measure(g) for g in groups]
        if max(widths) > max_width:
            continue
        spread = max(widths) - min(widths)
        if spread < best_spread:
            best_spread = spread
            best_split = tuple(groups)
    if best_split is None:
        return None
    return [" ".join(g) for g in best_split]


def _fit_text_with_pixel_bounds(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_filename: str,
    fonts_dir: str,
    min_size_px: int,
    max_size_px: int,
    box_w: int,
    target_h: int,
    preferred_line_count: int | None = None,
    min_line_count: int | None = None,
    max_line_count: int | None = None,
    line_spacing: float = 1.18,
    hero_scale_threshold_px: int = 80,
):
    """Find the largest font size in ``[min_size_px, max_size_px]`` whose
    wrapped text fits inside ``target_h`` and produces a line count between
    ``min_line_count`` and ``max_line_count`` (inclusive, when given).

    Selection priority:
      1. Sizes producing exactly ``preferred_line_count`` lines (largest wins)
      2. Sizes producing line counts within [min, max] bounds (largest wins)
      3. Fall back to the largest size that fits ``target_h`` regardless of line count
      4. Fall back to ``min_size_px`` if nothing fits (allow vertical overflow)

    Returns: (font, lines, total_text_height, scale_reason)
    where scale_reason is a short string describing which branch was taken.
    """
    if max_size_px < min_size_px:
        max_size_px = min_size_px

    # Each candidate is (size, font, lines, total_h, strategy).
    # ``strategy`` is "greedy" by default; the loop also tries
    # ``_wrap_text_balanced`` when greedy produces single-word lines on a
    # multi-word headline, so balanced variants compete head-to-head with
    # greedy at every size.
    candidates: list[tuple[int, ImageFont.ImageFont, list[str], int, str]] = []
    word_count = len(text.split())

    def _has_single_word_line(lines_in: list[str]) -> bool:
        return any(len(line.split()) == 1 for line in lines_in)

    for size in range(max_size_px, min_size_px - 1, -2):
        font = _load_font(fonts_dir, font_filename, size)
        greedy_lines = _wrap_text(draw, text, font, box_w)
        lh = _line_height(draw, font)
        greedy_h = int(lh * line_spacing) * len(greedy_lines)
        if greedy_h <= target_h:
            candidates.append((size, font, greedy_lines, greedy_h, "greedy"))

        # Balanced-wrap competition: only when greedy looks choppy
        # (a single-word line on a >2-word headline) and we have a target
        # line count to aim for.
        if (
            word_count > 2
            and _has_single_word_line(greedy_lines)
        ):
            targets: list[int] = []
            if preferred_line_count is not None:
                targets.append(preferred_line_count)
            if (
                preferred_line_count is None
                or len(greedy_lines) != preferred_line_count
            ):
                targets.append(len(greedy_lines))
            seen_targets: set[int] = set()
            for target_n in targets:
                if target_n in seen_targets or target_n < 1 or target_n > word_count:
                    continue
                seen_targets.add(target_n)
                balanced = _wrap_text_balanced(draw, text, font, box_w, target_n)
                if balanced is None:
                    continue
                bal_h = int(lh * line_spacing) * len(balanced)
                if bal_h <= target_h:
                    candidates.append((size, font, balanced, bal_h, "balanced"))

    if not candidates:
        font = _load_font(fonts_dir, font_filename, min_size_px)
        lines = _wrap_text(draw, text, font, box_w)
        lh = _line_height(draw, font)
        return (
            font, lines, int(lh * line_spacing) * len(lines),
            f"min_floor_overflow (no size in [{min_size_px}, {max_size_px}] fit target_h={target_h})",
            "greedy",
        )

    def _within_bounds(n: int) -> bool:
        if min_line_count is not None and n < min_line_count:
            return False
        if max_line_count is not None and n > max_line_count:
            return False
        return True

    # Selection priority:
    #   - matches preferred_line_count (tier 1)
    #   - within [min_line_count, max_line_count] (tier 2)
    #   - any fitting size (tier 3)
    # Within each tier:
    #   - prefer wraps WITHOUT single-word lines on multi-word headlines
    #     (avoids the "Refresh / your / summer, / naturally." failure
    #     mode); a 58 px clean 3-line wrap beats a 70 px 4-single-word wrap
    #   - then largest font size
    #   - then balanced strategy on a tie
    has_choppy_threshold = word_count > 2
    # When NO candidate has zero single-word lines (narrow box + long
    # words), single-word lines aren't avoidable — the fitter switches
    # to "hero-scale" mode where a font ≥ ``hero_scale_threshold_px`` is
    # treated as intentional poster typography (better than caption-sized
    # multi-word wraps). The non-choppy preference still wins whenever a
    # zero-single-word-line wrap exists.
    any_zero_choppy_candidate = any(
        sum(1 for line in c[2] if len(line.split()) == 1) == 0
        for c in candidates
    )

    def _rank(c: tuple[int, ImageFont.ImageFont, list[str], int, str]) -> tuple:
        size, _font, lines, _h, strategy = c
        single_word_lines = sum(1 for line in lines if len(line.split()) == 1)
        if not has_choppy_threshold:
            choppy_score = 0
        elif any_zero_choppy_candidate:
            # A non-choppy alternative exists somewhere — penalize
            # single-word lines hard so the tidy wrap wins.
            choppy_score = -single_word_lines
        else:
            # Single-word lines are unavoidable. Score by hero-scale
            # adjacency: a 100-px 4-single-word stack reads as poster
            # typography; a 56-px version reads as caption-sized.
            # Sizes at or above the threshold tier above smaller ones.
            choppy_score = 1 if size >= hero_scale_threshold_px else 0
        # Higher tuple = better. Sort descending.
        return (
            choppy_score,
            size,
            0 if strategy == "greedy" else 1,
        )

    # 1. Preferred line count, if achievable AND in bounds.
    if preferred_line_count is not None and _within_bounds(preferred_line_count):
        preferred = [c for c in candidates if len(c[2]) == preferred_line_count]
        if preferred:
            size, font, lines, total_h, strategy = max(preferred, key=_rank)
            return font, lines, total_h, (
                f"preferred_line_count={preferred_line_count} at largest fitting size ({size}px, strategy={strategy})"
            ), strategy

    # 2. Within line-count bounds.
    if min_line_count is not None or max_line_count is not None:
        bounded = [c for c in candidates if _within_bounds(len(c[2]))]
        if bounded:
            size, font, lines, total_h, strategy = max(bounded, key=_rank)
            return font, lines, total_h, (
                f"largest size honoring line bounds [{min_line_count}, {max_line_count}] ({size}px, {len(lines)} lines, strategy={strategy})"
            ), strategy

    # 3. Largest fitting size, line count whatever it is.
    size, font, lines, total_h, strategy = max(candidates, key=_rank)
    return font, lines, total_h, (
        f"largest fitting size ({size}px, {len(lines)} lines, strategy={strategy})"
    ), strategy


def _fit_text_to_box(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_filename: str,
    fonts_dir: str,
    initial_size: int,
    box_w: int,
    box_h: int,
    line_spacing: float = 1.18,
    min_size: int | None = None,
):
    """Auto-shrink the font until wrapped lines fit inside the box.
    Returns (font, lines, total_text_height)."""
    min_size = min_size or _FALLBACKS["min_font_size"]
    size = initial_size
    while size >= min_size:
        font = _load_font(fonts_dir, font_filename, size)
        lines = _wrap_text(draw, text, font, box_w)
        lh = _line_height(draw, font)
        total_h = int(lh * line_spacing) * len(lines)
        if total_h <= box_h or size <= min_size:
            return font, lines, total_h
        size = int(size * 0.93)
    # Should not reach here; guard.
    font = _load_font(fonts_dir, font_filename, min_size)
    lines = _wrap_text(draw, text, font, box_w)
    lh = _line_height(draw, font)
    return font, lines, int(lh * line_spacing) * len(lines)


def smart_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Backwards-compatible wrapper around the scoring-based crop. Older
    tests use ``smart_crop(img, w, h)`` without layout context; that path
    falls through to the legacy center-with-upward-bias behavior."""
    cropped, _ = smart_crop_with_scoring(
        img, target_w, target_h, layout=None, ratio=None, brand=None,
    )
    return cropped


def _crop_focal_after_offset(
    img: Image.Image, crop_box_px: tuple[int, int, int, int],
    target_w: int, target_h: int,
) -> tuple[tuple[float, float, float, float] | None, float]:
    """Estimate focal area on the resized crop. Returns (focal_pct,
    focal_density). Reuses the same coarse-grid heuristic the rest of the
    composer relies on so crop-time and post-overlay-time agree."""
    cropped = img.crop(crop_box_px).resize((target_w, target_h), Image.LANCZOS)
    return _estimate_focal_area(cropped)


def _focal_edge_clearance_metrics(
    focal_pct: tuple[float, float, float, float] | None,
    target_w: int,
    target_h: int,
    edge_pad_px: int,
) -> dict:
    """Return per-edge gap (px) from focal box to the canvas edges and a
    pass/clip summary."""
    if focal_pct is None:
        return {
            "focal_edge_gap_px": None,
            "min_edge_gap_px": None,
            "focal_edge_clearance_pass": True,
            "focal_edge_clip_detected": False,
            "edges_touched": [],
        }
    fx0, fy0, fx1, fy1 = focal_pct
    left_px = fx0 * target_w
    right_px = (1.0 - fx1) * target_w
    top_px = fy0 * target_h
    bottom_px = (1.0 - fy1) * target_h
    edges = {
        "left": left_px,
        "right": right_px,
        "top": top_px,
        "bottom": bottom_px,
    }
    min_gap = min(edges.values())
    edges_touched = [name for name, gap in edges.items() if gap < edge_pad_px]
    clip = min_gap <= 0.0  # focal touches a canvas edge → clipped
    return {
        "focal_edge_gap_px": {k: round(v, 1) for k, v in edges.items()},
        "min_edge_gap_px": round(min_gap, 1),
        "focal_edge_clearance_pass": (min_gap >= edge_pad_px),
        "focal_edge_clip_detected": clip,
        "edges_touched": edges_touched,
    }


def smart_crop_with_scoring(
    img: Image.Image,
    target_w: int,
    target_h: int,
    layout: "LayoutTemplate | None",
    ratio: str | None,
    brand: "BrandGuidelines | None" = None,
) -> tuple[Image.Image, dict]:
    """Score a deterministic set of crop offsets and pick whichever crop
    keeps the estimated focal/product cluster furthest from the canvas
    edges. Falls back to legacy center-bias behavior when the source
    already matches the target aspect or no layout is supplied.

    Returns ``(cropped_canvas, crop_meta)``. ``crop_meta`` contains:
      strategy_used, candidates, scores, edge_gap_px, clearance_pass, etc.
    """
    src_w, src_h = img.size
    target_aspect = target_w / target_h
    src_aspect = src_w / src_h
    base_meta = {
        "crop_strategy_used": None,
        "crop_box_used": None,
        "crop_box_candidates": [],
        "crop_box_scores": [],
        "focal_edge_gap_px": None,
        "min_edge_gap_px": None,
        "focal_edge_clearance_pass": True,
        "focal_edge_clip_detected": False,
        "crop_edge_clip_penalty_applied": False,
        "edges_touched": [],
        "letterbox_applied": False,
        "letterbox_pad_pct": None,
        "letterbox_color_used": None,
        "letterbox_color_source": None,
    }

    # Aspect already matches → no crop needed.
    if abs(src_aspect - target_aspect) < 0.01:
        canvas = img.resize((target_w, target_h), Image.LANCZOS)
        focal_pct, _ = _estimate_focal_area(canvas)
        edge_pad_px = _resolve_edge_pad_px(layout, ratio, target_w, target_h)
        edge = _focal_edge_clearance_metrics(focal_pct, target_w, target_h, edge_pad_px)
        return canvas, {**base_meta, "crop_strategy_used": "no_crop_aspect_match", **edge}

    # Build deterministic crop candidates spanning the valid offset range.
    candidates: list[tuple[str, tuple[int, int, int, int]]] = []
    if src_aspect > target_aspect:
        # Source is wider than target → vary x offset (vertical band).
        new_w = int(src_h * target_aspect)
        max_x0 = src_w - new_w
        for label, frac in (
            ("center", 0.50), ("upper_left", 0.30), ("right", 0.70),
            ("left", 0.15), ("far_right", 0.85),
        ):
            x0 = max(0, min(max_x0, int(max_x0 * frac)))
            candidates.append((f"x_{label}_{frac:.2f}", (x0, 0, x0 + new_w, src_h)))
    else:
        # Source is taller than target → vary y offset.
        new_h = int(src_w / target_aspect)
        max_y0 = src_h - new_h
        for label, frac in (
            ("upper", 0.20), ("center", 0.50), ("upper_third", 0.10),
            ("lower", 0.70), ("top", 0.05),
        ):
            y0 = max(0, min(max_y0, int(max_y0 * frac)))
            candidates.append((f"y_{label}_{frac:.2f}", (0, y0, src_w, y0 + new_h)))

    edge_pad_px = _resolve_edge_pad_px(layout, ratio, target_w, target_h)
    hard_fail = bool(layout and layout.hard_fail_focal_edge_clip)
    clip_penalty = (
        layout.crop_edge_clip_penalty if layout is not None else 0.0
    )

    scored: list[dict] = []
    for label, crop_box_px in candidates:
        focal_pct, focal_density = _crop_focal_after_offset(
            img, crop_box_px, target_w, target_h,
        )
        edge = _focal_edge_clearance_metrics(focal_pct, target_w, target_h, edge_pad_px)
        # Score: clearance pass = +1.0, near-edge gradient otherwise.
        # Penalize hard clip; reward whichever crop has the largest min-edge gap.
        if focal_pct is None:
            base_score = 0.5  # no detected focal — neutral
        else:
            min_gap = edge["min_edge_gap_px"]
            base_score = min(1.0, max(0.0, min_gap / max(1, edge_pad_px * 2)))
        if edge["focal_edge_clip_detected"]:
            base_score -= clip_penalty
        scored.append({
            "label": label,
            "crop_box_px": list(crop_box_px),
            "focal_pct": list(focal_pct) if focal_pct else None,
            "focal_density": focal_density,
            "min_edge_gap_px": edge["min_edge_gap_px"],
            "edges_touched": edge["edges_touched"],
            "clearance_pass": edge["focal_edge_clearance_pass"],
            "clip_detected": edge["focal_edge_clip_detected"],
            "score": round(base_score, 4),
        })

    # Prefer the highest-scoring viable crop. When hard_fail is on and
    # nothing is viable, the highest score still wins (least bad), but
    # the meta records that the penalty was applied.
    viable = [s for s in scored if (not hard_fail) or (not s["clip_detected"])]
    pool = viable or scored
    winner = max(pool, key=lambda s: s["score"])
    crop_box_px = tuple(winner["crop_box_px"])
    cropped = img.crop(crop_box_px).resize((target_w, target_h), Image.LANCZOS)

    # Re-run edge metrics on the *winner* for the meta (the focal estimate
    # is identical to what the scorer saw).
    final_focal_pct = (
        tuple(winner["focal_pct"]) if winner["focal_pct"] else None
    )
    final_edge = _focal_edge_clearance_metrics(
        final_focal_pct, target_w, target_h, edge_pad_px,
    )

    # Letterbox fallback: when no crop offset clears the focal cluster
    # from the canvas edges, fit the source onto a neutral-color canvas
    # so the focal product gets visible breathing room. Only fires when
    # the brand explicitly opts in and the required pad fits the cap.
    letterbox_meta: dict = {
        "letterbox_applied": False,
        "letterbox_pad_pct": None,
        "letterbox_color_used": None,
        "letterbox_color_source": None,
    }
    needs_letterbox = (
        layout is not None
        and getattr(layout, "enable_focal_letterbox_when_clip_unavoidable", False)
        and bool(scored)
        and all(s["clip_detected"] for s in scored)
    )
    if needs_letterbox:
        current_gap = winner["min_edge_gap_px"] or 0
        # Pad needed to lift the focal off the canvas edge to the brand
        # threshold, expressed as a fraction of min canvas dim.
        gap_deficit_px = max(0, edge_pad_px - current_gap)
        target_dim = min(target_w, target_h)
        required_pad_pct = gap_deficit_px / max(1, target_dim)
        max_pad = getattr(layout, "letterbox_max_pad_pct", 0.08)
        if 0 < required_pad_pct <= max_pad:
            # Pick the fill color per ``letterbox_color_role``.
            color_role = getattr(layout, "letterbox_color_role", "auto")
            color_used: str
            color_source: str
            if color_role == "brand_primary":
                color_used = brand.visual_identity.primary_color if brand else "#FFFFFF"
                color_source = "brand_explicit"
            elif color_role == "brand_secondary":
                color_used = brand.visual_identity.secondary_color if brand else "#FFFFFF"
                color_source = "brand_explicit"
            elif color_role == "brand_accent":
                color_used = brand.visual_identity.accent_color if brand else "#FFFFFF"
                color_source = "brand_explicit"
            elif color_role == "sampled_edge":
                color_used = "#{:02X}{:02X}{:02X}".format(*_sample_edge_median_color(img))
                color_source = "sampled"
            else:  # auto
                sampled_rgb = _sample_edge_median_color(img)
                if brand is not None:
                    palette = [
                        brand.visual_identity.primary_color,
                        brand.visual_identity.secondary_color,
                        brand.visual_identity.accent_color,
                    ]
                    snapped, dist = _snap_color_to_brand(sampled_rgb, palette)
                else:
                    snapped, dist = None, float("inf")
                if snapped is not None:
                    color_used = snapped
                    color_source = "brand_snap"
                else:
                    color_used = "#{:02X}{:02X}{:02X}".format(*sampled_rgb)
                    color_source = "sampled"

            letterbox_canvas, lb_meta = _letterbox_fit_fallback(
                img, target_w, target_h,
                fill_rgb=_hex_to_rgb(color_used),
                pad_pct=required_pad_pct,
            )
            cropped = letterbox_canvas.convert("RGBA")
            # Re-estimate focal on the letterboxed canvas — its edges should
            # now clear the brand threshold by construction.
            final_focal_pct, _ = _estimate_focal_area(cropped)
            final_edge = _focal_edge_clearance_metrics(
                final_focal_pct, target_w, target_h, edge_pad_px,
            )
            letterbox_meta = {
                "letterbox_applied": True,
                "letterbox_pad_pct": lb_meta["pad_pct"],
                "letterbox_color_used": color_used,
                "letterbox_color_source": color_source,
            }

    return cropped, {
        "crop_strategy_used": (
            f"scored_{len(scored)}_candidates: winner={winner['label']} "
            f"score={winner['score']}"
            + (" → letterbox_fallback" if letterbox_meta["letterbox_applied"] else "")
        ),
        "crop_box_used": list(crop_box_px),
        "crop_box_candidates": [
            {"label": s["label"], "crop_box_px": s["crop_box_px"]} for s in scored
        ],
        "crop_box_scores": scored,
        **final_edge,
        "crop_edge_clip_penalty_applied": (
            winner["clip_detected"]
            and clip_penalty > 0.0
            and not letterbox_meta["letterbox_applied"]
        ),
        **letterbox_meta,
    }


def _sample_edge_median_color(img: Image.Image, edge_pct: float = 0.04) -> tuple[int, int, int]:
    """Median RGB of the source's outer-ring pixels. Used by the letterbox
    fallback to pick a fill color that visually extends the source rather
    than dropping a cliff at the border."""
    import numpy as np
    rgb = img.convert("RGB")
    W, H = rgb.size
    edge = max(1, int(min(W, H) * edge_pct))
    pieces = [
        np.asarray(rgb.crop((0, 0, W, edge))).reshape(-1, 3),
        np.asarray(rgb.crop((0, H - edge, W, H))).reshape(-1, 3),
        np.asarray(rgb.crop((0, 0, edge, H))).reshape(-1, 3),
        np.asarray(rgb.crop((W - edge, 0, W, H))).reshape(-1, 3),
    ]
    pixels = np.concatenate(pieces)
    median = np.median(pixels, axis=0).astype(int)
    return (int(median[0]), int(median[1]), int(median[2]))


def _snap_color_to_brand(
    rgb: tuple[int, int, int],
    brand_colors_hex: list[str],
    threshold: float = 60.0,
) -> tuple[str | None, float]:
    """Snap an arbitrary RGB to the nearest brand hex if within an RGB
    Euclidean distance of ``threshold``. Returns ``(hex_or_None, distance)``."""
    best_hex: str | None = None
    best_dist: float = float("inf")
    for hex_color in brand_colors_hex:
        br, bg, bb = _hex_to_rgb(hex_color)
        d = ((rgb[0] - br) ** 2 + (rgb[1] - bg) ** 2 + (rgb[2] - bb) ** 2) ** 0.5
        if d < best_dist:
            best_dist = d
            best_hex = hex_color
    if best_hex is not None and best_dist <= threshold:
        return best_hex, best_dist
    return None, best_dist


def _letterbox_fit_fallback(
    img: Image.Image,
    target_w: int,
    target_h: int,
    fill_rgb: tuple[int, int, int],
    pad_pct: float,
) -> tuple[Image.Image, dict]:
    """Downscale the entire source by ``2 * pad_pct`` and paste centered on
    a solid-color canvas. Used when no crop offset clears the focal cluster
    from the canvas edges."""
    pad_w = int(target_w * pad_pct)
    pad_h = int(target_h * pad_pct)
    inner_w = max(1, target_w - 2 * pad_w)
    inner_h = max(1, target_h - 2 * pad_h)
    src_w, src_h = img.size
    scale = min(inner_w / src_w, inner_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    fitted = img.convert("RGBA").resize((new_w, new_h), Image.LANCZOS)
    canvas = Image.new("RGBA", (target_w, target_h), fill_rgb + (255,))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    canvas.alpha_composite(fitted, dest=(paste_x, paste_y))
    return canvas, {
        "pad_pct": round(pad_pct, 4),
        "pad_px": [pad_w, pad_h],
        "inner_size_px": [new_w, new_h],
        "paste_at_px": [paste_x, paste_y],
    }


def _resolve_edge_pad_px(
    layout: "LayoutTemplate | None", ratio: str | None,
    target_w: int, target_h: int,
) -> int:
    """Compute the focal-edge breathing-room threshold from the layout.
    Falls back to a sensible default (4% of min dim) when no layout."""
    if layout is None:
        return int(0.04 * min(target_w, target_h))
    px_min = layout.min_focal_edge_gap_px.get(ratio or "", 0) if ratio else 0
    pct_min = int(layout.focal_edge_clearance_pct * min(target_w, target_h))
    return max(px_min, pct_min)


# ----- overlay rendering ----------------------------------------------------


def _render_overlay(canvas: Image.Image, layout: LayoutTemplate) -> Image.Image:
    """Paint the configured overlay onto an RGBA canvas. Mutates a copy."""
    W, H = canvas.size
    style = layout.overlay_style
    if style == OverlayStyle.NONE:
        return canvas

    opacity_max = int(layout.overlay_opacity() * 255)
    extent = layout.overlay_extent()  # 0..1, fraction of canvas

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    if style == OverlayStyle.SCRIM:
        # Solid band across the bottom `extent` of the canvas.
        band_h = int(H * extent)
        ImageDraw.Draw(overlay).rectangle(
            (0, H - band_h, W, H), fill=(0, 0, 0, opacity_max)
        )
    elif style == OverlayStyle.VERTICAL_GRADIENT:
        # Linear: 0 alpha at (1 - extent) of canvas, opacity_max at the bottom.
        band_top = int(H * (1.0 - extent))
        band_h = max(1, H - band_top)
        # Build a single-column 1×band_h mask, then expand to width.
        col = Image.new("L", (1, band_h))
        for y in range(band_h):
            t = y / max(1, band_h - 1)  # 0 at top of band, 1 at bottom
            col.putpixel((0, y), int(opacity_max * (t ** 1.4)))  # gentle ease-in
        col = col.resize((W, band_h), Image.LANCZOS)
        alpha = Image.new("L", (W, H), 0)
        alpha.paste(col, (0, band_top))
        black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        black.putalpha(alpha)
        overlay = black
    elif style == OverlayStyle.DIAGONAL_GRADIENT:
        # Strongest at bottom-left, fading toward upper-right.
        band_top = int(H * (1.0 - extent))
        band_h = max(1, H - band_top)
        alpha = Image.new("L", (W, H), 0)
        # Diagonal sweep across the lower band.
        for y in range(band_top, H):
            row = Image.new("L", (W, 1))
            ty = (y - band_top) / max(1, band_h - 1)
            for x in range(0, W, max(1, W // 256)):  # subsample for speed
                tx = 1.0 - (x / max(1, W - 1))      # 1 at left, 0 at right
                t = max(0.0, min(1.0, (ty * 0.7 + tx * 0.3)))
                row.putpixel((x, 0), int(opacity_max * (t ** 1.3)))
            row = row.resize((W, 1), Image.LANCZOS)
            alpha.paste(row, (0, y))
        black = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        black.putalpha(alpha)
        overlay = black

    return Image.alpha_composite(canvas, overlay)


# ----- accent ----------------------------------------------------------------


def _resolve_accent_safe_x(
    canvas_w: int, canvas_h: int, layout: LayoutTemplate, ratio: str,
    desired_x: int, thickness: int,
) -> tuple[int, int]:
    """Return ``(rail_x_left, rail_x_right)`` for a vertical accent rail
    with the brand-configured left safe margin enforced. The rail's
    left edge cannot sit closer to the canvas than
    ``max(min_accent_edge_gap_px, accent_safe_zone_pct * min(W, H))``.
    Caller still gets to pick the headline-relative anchor; this just
    pushes the rail away from the frame when it would otherwise hug it.
    """
    px_floor = layout.min_accent_edge_gap_px.get(ratio, 0)
    pct_floor = int(layout.accent_safe_zone_pct * min(canvas_w, canvas_h))
    safe_left = max(px_floor, pct_floor)
    rail_x_right = max(desired_x, safe_left + thickness)
    rail_x_left = max(safe_left, rail_x_right - thickness)
    # Guarantee a 1 px rail width even after clamping.
    if rail_x_right <= rail_x_left:
        rail_x_right = rail_x_left + thickness
    return (rail_x_left, rail_x_right)


def _render_accent(
    canvas: Image.Image,
    layout: LayoutTemplate,
    brand: BrandGuidelines,
    text_box_px: tuple[int, int, int, int],
    headline_top_px: int,
    headline_bottom_px: int,
    ratio: str | None = None,
) -> tuple[Image.Image, dict]:
    """Render the layout's accent treatment. Returns the canvas + a meta
    dict (accent_line_box, accent_edge_gap_px, accent_safe_zone_pass) so
    the caller can audit the rail's left-margin compliance."""
    accent_meta = {
        "accent_line_box": None,
        "accent_edge_gap_px": None,
        "accent_safe_zone_pass": True,
    }
    if layout.accent_style == AccentStyle.NONE:
        return canvas, accent_meta

    W, H = canvas.size
    color_hex = _resolve_role_color(layout.accent_color_role, brand)
    color_rgba = _hex_to_rgb(color_hex) + (255,)
    thickness = max(2, int(min(W, H) * layout.accent_thickness_pct))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x0, y0, x1, y1 = text_box_px

    if layout.accent_style == AccentStyle.SIDE_RAIL:
        # Vertical rail flush with the left of the headline box, but
        # never inside the brand's left safe margin.
        desired_right = x0 - thickness
        rail_x_left, rail_x_right = _resolve_accent_safe_x(
            W, H, layout, ratio or "", desired_right, thickness,
        )
        draw.rectangle(
            (rail_x_left, headline_top_px, rail_x_right, headline_bottom_px),
            fill=color_rgba,
        )
        edge_gap = rail_x_left
        px_floor = layout.min_accent_edge_gap_px.get(ratio or "", 0)
        pct_floor = int(layout.accent_safe_zone_pct * min(W, H))
        accent_meta = {
            "accent_line_box": [rail_x_left, headline_top_px, rail_x_right, headline_bottom_px],
            "accent_edge_gap_px": edge_gap,
            "accent_safe_zone_pass": edge_gap >= max(px_floor, pct_floor),
        }
    elif layout.accent_style == AccentStyle.UNDERLINE:
        underline_w = min(int((x1 - x0) * 0.35), int(W * 0.18))
        y = headline_bottom_px + int(thickness * 1.2)
        draw.rectangle((x0, y, x0 + underline_w, y + thickness), fill=color_rgba)
    elif layout.accent_style == AccentStyle.COLOR_BLOCK:
        block_w = max(thickness * 4, int(W * 0.04))
        block_h = max(thickness * 4, headline_bottom_px - headline_top_px)
        draw.rectangle(
            (x0 - block_w - thickness, headline_top_px, x0 - thickness, headline_top_px + block_h),
            fill=color_rgba,
        )
    elif layout.accent_style == AccentStyle.SOFT_GLOW:
        # Soft elliptical glow behind the headline.
        glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        cx = (x0 + x1) // 2
        cy = (headline_top_px + headline_bottom_px) // 2
        rx = (x1 - x0) // 2
        ry = (headline_bottom_px - headline_top_px)
        gd.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color_rgba[:3] + (90,))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=int(min(W, H) * 0.04)))
        overlay = Image.alpha_composite(overlay, glow)

    return Image.alpha_composite(canvas, overlay), accent_meta


# ----- logo (with optional badge) -------------------------------------------


_LOGO_BADGE_SUPERSAMPLE = 4


def _render_logo_badge_supersampled(
    logo_source: Image.Image,
    target_w: int,
    target_h: int,
    pad: int,
    badge_color_rgba: tuple[int, int, int, int],
    supersample: int = _LOGO_BADGE_SUPERSAMPLE,
) -> Image.Image:
    """Render the circular/rounded logo badge at ``supersample`` × the final
    pixel size, then downsample to the final size with LANCZOS.

    Pillow's ``rounded_rectangle`` paints solid pixels with no anti-aliasing
    on the edge; at small final sizes (~100–150 px) the circle visibly
    stair-steps. Drawing 4× larger and resampling down with LANCZOS produces
    a smooth gradient on the perimeter. The inner logo is also resized
    directly from native source to ``supersample × target`` (one resample
    pass instead of two) for best quality.

    Returns an RGBA badge image at the requested final dimensions.
    """
    badge_w = target_w + pad * 2
    badge_h = target_h + pad * 2

    s = max(1, int(supersample))
    sw, sh = badge_w * s, badge_h * s

    big_badge = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    ImageDraw.Draw(big_badge).rounded_rectangle(
        (0, 0, sw, sh),
        radius=int(min(sw, sh) * 0.5),
        fill=badge_color_rgba,
    )

    # Resize the logo directly from its native resolution to the supersampled
    # target inside the badge — single LANCZOS pass.
    big_logo = logo_source.resize((target_w * s, target_h * s), Image.LANCZOS)
    big_badge.alpha_composite(big_logo, dest=(pad * s, pad * s))

    # Downsample the entire badge to the final size — this is what produces
    # the smooth anti-aliased edge on the circular outline.
    return big_badge.resize((badge_w, badge_h), Image.LANCZOS)


def _logo_position(W: int, H: int, w: int, h: int, placement: LogoPlacement, safe_x: int, safe_y: int) -> tuple[int, int]:
    if placement == LogoPlacement.TOP_LEFT:
        return (safe_x, safe_y)
    if placement == LogoPlacement.TOP_RIGHT:
        return (W - w - safe_x, safe_y)
    if placement == LogoPlacement.BOTTOM_LEFT:
        return (safe_x, H - h - safe_y)
    return (W - w - safe_x, H - h - safe_y)


def _compute_logo_footprint(
    canvas_w: int, canvas_h: int, brand: "BrandGuidelines",
) -> tuple[int, int]:
    """Return the on-canvas (width, height) of the rendered logo (badge
    or plain). Mirrors the size logic in ``stamp_logo`` so position
    selection can run *before* the actual paste."""
    vi = brand.visual_identity
    if not os.path.exists(vi.logo_path):
        return (0, 0)
    logo_source = Image.open(vi.logo_path).convert("RGBA")
    target_h = int(min(canvas_w, canvas_h) * (vi.logo_height_pct or _FALLBACKS["logo_height_pct"]))
    aspect = logo_source.size[0] / max(1, logo_source.size[1])
    target_w = max(1, int(target_h * aspect))
    if vi.logo_treatment == LogoTreatment.BADGE:
        pad = int(target_h * 0.18)
        return (target_w + pad * 2, target_h + pad * 2)
    return (target_w, target_h)


def _logo_bbox_at_placement(
    W: int, H: int, w: int, h: int,
    placement: LogoPlacement, safe_x: int, safe_y: int,
) -> tuple[int, int, int, int]:
    """Pixel bbox the logo would occupy at ``placement`` (top-left,
    top-right, bottom-left, bottom-right)."""
    pos = _logo_position(W, H, w, h, placement, safe_x, safe_y)
    return (pos[0], pos[1], pos[0] + w, pos[1] + h)


def select_logo_position(
    canvas_w: int,
    canvas_h: int,
    logo_w: int,
    logo_h: int,
    brand: "BrandGuidelines",
    ratio: str,
    focal_safe_zone_pct: tuple[float, float, float, float] | None,
) -> dict:
    """Pick the logo placement that keeps clear breathing room from the
    focal/product safe zone.

    Order:
      1. configured ``logo_placement`` (always tried first)
      2. each entry in ``logo_allowed_positions`` (in YAML order)
    First placement whose logo bbox achieves the required gap to the
    focal safe zone wins. When none clear the bar, returns the configured
    placement with ``selection_reason`` flagging the warning.
    """
    vi = brand.visual_identity
    safe_x = int(canvas_w * vi.safe_zone_pct)
    safe_y = int(canvas_h * vi.safe_zone_pct)
    placements: list[LogoPlacement] = [vi.logo_placement]
    for p in vi.logo_allowed_positions:
        if p not in placements:
            placements.append(p)

    min_gap_px = vi.min_logo_product_gap_px.get(ratio, 0)
    min_pct_gap_px = int(vi.logo_product_clearance_pct * min(canvas_w, canvas_h))
    required_gap = max(min_gap_px, min_pct_gap_px)
    hard_fail = vi.hard_fail_logo_product_collision

    attempts: list[dict] = []
    accepted: dict | None = None
    for placement in placements:
        bbox_px = _logo_bbox_at_placement(
            canvas_w, canvas_h, logo_w, logo_h, placement, safe_x, safe_y,
        )
        bbox_pct = (
            bbox_px[0] / max(1, canvas_w),
            bbox_px[1] / max(1, canvas_h),
            bbox_px[2] / max(1, canvas_w),
            bbox_px[3] / max(1, canvas_h),
        )
        if focal_safe_zone_pct is not None:
            overlap = _box_overlap_pct(bbox_pct, focal_safe_zone_pct)
            gap = _box_gap_px(bbox_pct, focal_safe_zone_pct, canvas_w, canvas_h)
        else:
            overlap, gap = 0.0, float("inf")
        collision = overlap > 0.0
        near_miss = (not collision) and (gap < required_gap)
        clearance_pass = (not collision) and (not near_miss)
        attempt = {
            "placement": placement.value,
            "bbox_px": list(bbox_px),
            "gap_px": (
                None if gap == float("inf") else round(gap, 1)
            ),
            "collision": collision,
            "near_miss": near_miss,
            "clearance_pass": clearance_pass,
        }
        attempts.append(attempt)
        if clearance_pass and accepted is None:
            accepted = attempt
        if accepted is not None and (not hard_fail or accepted["clearance_pass"]):
            break

    if accepted is None:
        # No placement cleared — fall back to configured (least-bad).
        accepted = attempts[0]
        selection_reason = (
            f"no allowed placement cleared {required_gap}px gap "
            f"(configured={vi.logo_placement.value}); using configured anyway"
        )
    else:
        selection_reason = (
            f"first viable placement: {accepted['placement']} "
            f"(gap_px={accepted['gap_px']}, required={required_gap})"
        )

    return {
        "placement": LogoPlacement(accepted["placement"]),
        "bbox_px": tuple(accepted["bbox_px"]),
        "configured_placement": vi.logo_placement.value,
        "logo_position_selected": accepted["placement"],
        "logo_position_configured": vi.logo_placement.value,
        "logo_position_adjusted": accepted["placement"] != vi.logo_placement.value,
        "logo_product_gap_px": accepted["gap_px"],
        "logo_product_clearance_pass": accepted["clearance_pass"],
        "logo_collision_detected": accepted["collision"],
        "logo_selection_reason": selection_reason,
        "logo_position_attempts": attempts,
        "logo_min_required_gap_px": required_gap,
    }


def stamp_logo(
    img: Image.Image,
    brand: BrandGuidelines,
    placement_override: LogoPlacement | None = None,
) -> tuple[Image.Image, dict]:
    vi = brand.visual_identity
    if not os.path.exists(vi.logo_path):
        logger.warning("Logo file not found at %s — skipping logo stamp", vi.logo_path)
        return img, {"position": None, "size_px": None, "treatment": "missing"}

    canvas = img.convert("RGBA")
    W, H = canvas.size
    logo_source = Image.open(vi.logo_path).convert("RGBA")

    target_h = int(min(W, H) * (vi.logo_height_pct or _FALLBACKS["logo_height_pct"]))
    aspect = logo_source.size[0] / logo_source.size[1]
    target_w = max(1, int(target_h * aspect))

    safe_x = int(W * vi.safe_zone_pct)
    safe_y = int(H * vi.safe_zone_pct)
    placement = placement_override if placement_override is not None else vi.logo_placement

    if vi.logo_treatment == LogoTreatment.BADGE:
        pad = int(target_h * 0.18)
        badge_color_rgba = _hex_to_rgb(vi.logo_badge_color) + (int(vi.logo_badge_opacity * 255),)
        # Supersampled render: draws the rounded rectangle and the inner logo
        # at 4× scale, then LANCZOS-downsamples to final size. Eliminates the
        # jagged edge that direct same-size rounded_rectangle produces.
        badge = _render_logo_badge_supersampled(
            logo_source=logo_source,
            target_w=target_w,
            target_h=target_h,
            pad=pad,
            badge_color_rgba=badge_color_rgba,
        )
        badge_w, badge_h = badge.size
        pos = _logo_position(W, H, badge_w, badge_h, placement, safe_x, safe_y)
        canvas.alpha_composite(badge, dest=pos)
        logo_w, logo_h = badge_w, badge_h
    else:
        # Plain treatment: single LANCZOS resize of the source logo.
        logo = logo_source.resize((target_w, target_h), Image.LANCZOS)
        pos = _logo_position(W, H, target_w, target_h, placement, safe_x, safe_y)
        canvas.alpha_composite(logo, dest=pos)
        logo_w, logo_h = target_w, target_h

    return canvas, {
        "position": list(pos),
        "size_px": [logo_w, logo_h],
        "treatment": vi.logo_treatment.value,
        "placement": placement.value,
    }


# ----- composition pipeline -------------------------------------------------


def _choose_text_treatment(
    cropped_hero: Image.Image,
    headline_box_px: tuple[int, int, int, int],
    layout: LayoutTemplate,
    brand: BrandGuidelines,
) -> tuple[Image.Image, str, float, float]:
    """Pick the highest-contrast text color and (optionally) escalate overlay
    opacity once when contrast falls below the brand's WCAG-style threshold.

    Reads brand.qc.* as **guidance only** — the composer tries to improve the
    odds of passing; ``QCCheckerAgent`` independently re-measures the rendered
    PNG and is the source of truth for pass/fail/halt.

    Strategy:
      1. Apply overlay at the layout's configured opacity.
      2. For both ``typography.text_color_on_dark`` and
         ``typography.text_color_on_light``, compute WCAG contrast vs. the
         actual post-overlay background sampled inside ``headline_box_px``.
      3. Keep the higher-contrast color.
      4. If ``required_brand_checks.contrast_ratio`` is enabled AND the chosen
         ratio is below ``qc.min_contrast_ratio`` (or ``qc.large_text_min_ratio``
         for large headlines), escalate overlay opacity once by +0.20 (capped
         at 0.95) and re-pick. Keep whichever pass produced the higher ratio.

    The +0.20 step / 0.95 ceiling are composer-internal *fallbacks* — the
    contrast thresholds themselves stay in YAML.

    Returns:
        (canvas_with_overlay, text_color_hex, final_overlay_opacity, final_contrast_ratio)
    """
    qc = brand.qc
    contrast_check_enabled = brand.required_brand_checks.contrast_ratio
    candidates = [
        brand.typography.text_color_on_dark,
        brand.typography.text_color_on_light,
    ]
    box_h = max(1, headline_box_px[3] - headline_box_px[1])
    is_large = box_h >= qc.large_text_size_threshold_px
    target_threshold = qc.large_text_min_ratio if is_large else qc.min_contrast_ratio

    def _box_mean_rgb(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int]:
        # Sample the actual background mean *without* filtering — the composer
        # runs before text is drawn, so there are no text pixels to exclude.
        # (estimate_background_color in tools.contrast is QC-side: it strips
        # pixels close to the rendered text color, which would bias the
        # composer's color comparison toward whichever color is being tested.)
        crop = image.crop(box).convert("RGB").resize((1, 1), Image.BOX)
        return crop.getpixel((0, 0))

    def _try_at(opacity: float) -> tuple[Image.Image, str, float, float]:
        adjusted = layout.model_copy(update={"overlay_opacity_pct": opacity})
        canvas = _render_overlay(cropped_hero, adjusted)
        bg_rgb = _box_mean_rgb(canvas, headline_box_px)
        scored: list[tuple[str, float]] = []
        for color_hex in candidates:
            text_rgb = _contrast.hex_to_rgb(color_hex)
            scored.append((color_hex, _contrast.contrast_ratio(text_rgb, bg_rgb)))
        scored.sort(key=lambda x: x[1], reverse=True)
        best_color, best_ratio = scored[0]
        return canvas, best_color, opacity, best_ratio

    base_opacity = layout.overlay_opacity()
    canvas, color, used_opacity, ratio = _try_at(base_opacity)

    if contrast_check_enabled and ratio < target_threshold:
        escalated = min(base_opacity + 0.20, 0.95)
        if escalated > base_opacity + 1e-3:
            canvas2, color2, opacity2, ratio2 = _try_at(escalated)
            if ratio2 > ratio:
                canvas, color, used_opacity, ratio = canvas2, color2, opacity2, ratio2

    return canvas, color, used_opacity, ratio


def _resolve_per_aspect(layout: LayoutTemplate, ratio: str) -> PerAspectLayout:
    if ratio in layout.per_aspect:
        return layout.per_aspect[ratio]
    return PerAspectLayout(
        headline_box=(0.05, 1.0 - layout.text_region_pct, 0.95, 0.97),
        text_align=layout.text_align_default,
    )


def _align_x(box_left: int, box_right: int, line_w: int, align: TextAlign) -> int:
    if align == TextAlign.LEFT:
        return box_left
    if align == TextAlign.RIGHT:
        return box_right - line_w
    return box_left + (box_right - box_left - line_w) // 2


def select_disclaimer_box(
    canvas: Image.Image,
    layout: "LayoutTemplate",
    ratio: str,
    focal_safe_zone_pct: tuple[float, float, float, float] | None,
    text_color_options: list[str],
    fallback_box_px: tuple[int, int, int, int],
) -> dict:
    """Pick the disclaimer placement that best preserves contrast +
    clearance from the focal/product safe zone. Iterates
    ``layout.disclaimer_candidate_boxes[ratio]`` (in YAML order) and
    scores each candidate. First candidate is the configured preference;
    subsequent ones are alternates the composer falls back to when the
    preferred zone is crowded.

    Returns dict with the chosen box plus full audit. When the layout
    doesn't supply candidates, falls through to ``fallback_box_px`` so
    legacy templates keep working.
    """
    W, H = canvas.size
    candidates_pct = layout.disclaimer_candidate_boxes.get(ratio, [])
    if not candidates_pct:
        return {
            "box_px": fallback_box_px,
            "candidate_pct": None,
            "candidate_index": None,
            "candidate_attempts": [],
            "selection_reason": "no_candidates_configured: using fallback placement",
            "clearance_pass": True,
            "object_gap_px": None,
            "contrast_estimate": None,
        }

    px_floor = layout.min_disclaimer_object_gap_px.get(ratio, 0)
    pct_floor = int(0.025 * min(W, H))
    required_gap = max(px_floor, pct_floor)

    attempts: list[dict] = []
    accepted: dict | None = None
    for idx, box_pct in enumerate(candidates_pct):
        bx0, by0, bx1, by1 = box_pct
        box_px = (
            int(bx0 * W), int(by0 * H), int(bx1 * W), int(by1 * H),
        )
        if focal_safe_zone_pct is not None:
            overlap = _box_overlap_pct(box_pct, focal_safe_zone_pct)
            gap = _box_gap_px(box_pct, focal_safe_zone_pct, W, H)
        else:
            overlap, gap = 0.0, float("inf")
        clearance_pass = (overlap == 0.0) and (gap >= required_gap)

        bg_rgb = _box_mean_rgb_ext(canvas, box_px)
        contrasts = sorted(
            ((c, _contrast.contrast_ratio(_contrast.hex_to_rgb(c), bg_rgb))
             for c in text_color_options),
            key=lambda x: x[1], reverse=True,
        )
        best_color, best_contrast = contrasts[0]

        attempt = {
            "candidate_index": idx,
            "box_pct": [round(c, 4) for c in box_pct],
            "box_px": list(box_px),
            "object_gap_px": (
                None if gap == float("inf") else round(gap, 1)
            ),
            "overlap": round(overlap, 4),
            "clearance_pass": clearance_pass,
            "contrast_estimate": round(best_contrast, 2),
            "best_text_color": best_color,
        }
        attempts.append(attempt)
        if clearance_pass and accepted is None:
            accepted = attempt
            break

    if accepted is None:
        # No candidate cleared. Pick whichever has the largest gap (least
        # crowded) so we report the least-bad choice.
        accepted = max(
            attempts,
            key=lambda a: (a["clearance_pass"], a["object_gap_px"] or 0),
        )
        selection_reason = (
            f"no candidate cleared {required_gap}px gap; "
            f"using least-crowded #{accepted['candidate_index']} "
            f"(gap={accepted['object_gap_px']})"
        )
    else:
        selection_reason = (
            f"candidate #{accepted['candidate_index']} "
            f"(gap={accepted['object_gap_px']}, contrast≈{accepted['contrast_estimate']})"
        )

    return {
        "box_px": tuple(accepted["box_px"]),
        "candidate_pct": accepted["box_pct"],
        "candidate_index": accepted["candidate_index"],
        "candidate_attempts": attempts,
        "selection_reason": selection_reason,
        "clearance_pass": accepted["clearance_pass"],
        "object_gap_px": accepted["object_gap_px"],
        "contrast_estimate": accepted["contrast_estimate"],
        "min_required_gap_px": required_gap,
    }


def _disclaimer_box(
    canvas_w: int,
    canvas_h: int,
    headline_box_px: tuple[int, int, int, int],
    headline_bottom_px: int,
    placement: DisclaimerPlacement,
    pad_pct: float,
) -> tuple[int, int, int, int]:
    pad_x = int(canvas_w * pad_pct)
    pad_y = int(canvas_h * pad_pct)
    if placement == DisclaimerPlacement.UNDER_HEADLINE:
        x0, _, x1, y1 = headline_box_px
        top = headline_bottom_px + max(8, int(canvas_h * 0.012))
        return (x0, min(top, y1), x1, y1)
    if placement == DisclaimerPlacement.BOTTOM_CENTER:
        return (
            pad_x,
            int(canvas_h * 0.93),
            canvas_w - pad_x,
            canvas_h - pad_y // 2,
        )
    # BOTTOM_CORNER → bottom-right by default; small width.
    box_w = int(canvas_w * 0.45)
    return (
        canvas_w - pad_x - box_w,
        int(canvas_h * 0.93),
        canvas_w - pad_x,
        canvas_h - pad_y // 2,
    )


def compose_creative(
    hero_path: str,
    ratio: str,
    headline: str,
    disclaimer: str | None,
    guidelines: BrandGuidelines,
    layout: LayoutTemplate,
    out_path: str,
) -> dict:
    """Compose one creative for one (ratio, market). Returns a metadata dict
    with full layout accounting for the report:
        path, ratio, size, duration_ms, disclaimer_rendered,
        headline_box, disclaimer_box, logo_box, logo_position, logo_size_px,
        overlay_style, overlay_opacity, accent_style, accent_color
    """
    aspect_dims = guidelines.aspect_ratios.get(ratio)
    if aspect_dims is None:
        raise ValueError(
            f"Aspect ratio {ratio!r} is not defined in brand.aspect_ratios. "
            f"Available: {list(guidelines.aspect_ratios.keys())}"
        )
    target_w, target_h = aspect_dims

    started = time.monotonic()
    hero = Image.open(hero_path).convert("RGBA")
    cropped, crop_meta = smart_crop_with_scoring(
        hero, target_w, target_h, layout=layout, ratio=ratio, brand=guidelines,
    )

    # Resolve per-aspect layout, then pick the cleanest available headline
    # zone. When the brand has configured ``headline_candidate_boxes`` and
    # ``avoid_busy_regions: true``, the composer scores each candidate
    # (contrast + texture stddev + Canny edge density) and picks the
    # winner; otherwise it uses the single ``headline_box`` as before.
    per_aspect = _resolve_per_aspect(layout, ratio)
    (
        selected_box_pct,
        headline_box_selection,
        all_candidate_scores,
        focal_meta,
    ) = _select_headline_box(
        cropped, layout, per_aspect, guidelines, target_w, target_h,
    )

    # Adaptive box widening: when the chosen candidate's edge is more
    # conservative than necessary (focal product is far from it), let the
    # box extend up to the safe-zone clearance limit. This is the lever
    # that turns a 410-px-wide candidate into a ~600-px box on photos
    # where the product is in the right third — the headline can then
    # fit at hero scale instead of wrapping to single-word lines.
    pre_widen_box_pct = selected_box_pct
    safe_zone_for_widen = focal_meta.get("product_safe_zone_box")
    if safe_zone_for_widen is not None:
        safe_zone_for_widen = tuple(safe_zone_for_widen)
    widen_min_gap_px = focal_meta.get("min_gap_px_threshold") or 0
    widen_box, widen_reason, widen_delta_pct = _widen_box_toward_focal_safe_zone(
        selected_box_pct, safe_zone_for_widen, widen_min_gap_px,
        target_w, target_h, max_widen_pct=layout.headline_widen_max_pct,
    )
    headline_box_widened = widen_box != pre_widen_box_pct
    if headline_box_widened:
        selected_box_pct = widen_box

    bx0, by0, bx1, by1 = selected_box_pct
    hb = (int(bx0 * target_w), int(by0 * target_h), int(bx1 * target_w), int(by1 * target_h))
    box_w = hb[2] - hb[0]
    box_h = hb[3] - hb[1]

    fonts_dir = guidelines.typography.fonts_dir or _FALLBACKS["fonts_dir"]
    headline_ratio = (
        layout.headline_size_ratio
        or guidelines.typography.headline_size_ratio
        or _FALLBACKS["headline_size_ratio"]
    )
    body_ratio = (
        layout.body_size_ratio
        or guidelines.typography.body_size_ratio
        or _FALLBACKS["body_size_ratio"]
    )

    cased_headline = _apply_case(headline, guidelines.typography.headline_case)

    # Reserve disclaimer height inside the headline box only when placement is
    # under_headline so we don't over-shrink the headline.
    reserve_for_disclaimer = (
        disclaimer is not None and layout.disclaimer_placement == DisclaimerPlacement.UNDER_HEADLINE
    )
    headline_max_h = box_h
    if reserve_for_disclaimer:
        headline_max_h = int(box_h * 0.78)

    # Per-aspect typography from brand guidelines (optional). When present,
    # the composer honors hard pixel min/max + a target zone-fill ratio.
    typo_per_aspect = guidelines.typography.per_aspect.get(ratio)

    if typo_per_aspect is not None:
        headline_min_px = typo_per_aspect.headline_min_font_size_px
        headline_max_px = typo_per_aspect.headline_max_font_size_px
        headline_target_h = int(headline_max_h * typo_per_aspect.headline_target_zone_fill_pct)
        # Don't let the zone-fill target push us below the brand's stated
        # minimum font size — the floor wins.
        preferred_lines = typo_per_aspect.preferred_line_count
    else:
        # Legacy path: clamp from headline_size_ratio at the safe-fallback floor.
        headline_min_px = _FALLBACKS["min_font_size"]
        headline_max_px = max(20, int(target_h * headline_ratio))
        headline_target_h = headline_max_h
        preferred_lines = None

    # Pre-measure the headline so we know the actual rendered text region.
    # No draw side-effects on the dummy canvas — we just need text metrics.
    _dummy = Image.new("RGB", (target_w, target_h))
    _dummy_draw = ImageDraw.Draw(_dummy)

    def _fit_in_box(box_pct_in: tuple[float, float, float, float]) -> dict:
        """Fit the headline inside ``box_pct_in`` using the brand's per-aspect
        bounds. Returns dict with font/lines/total_h/scale_reason and the
        rendered text bbox (post-wrap, post-alignment, in pixel coords)."""
        bx0_, by0_, bx1_, by1_ = box_pct_in
        box_px_ = (
            int(bx0_ * target_w), int(by0_ * target_h),
            int(bx1_ * target_w), int(by1_ * target_h),
        )
        bw_ = max(1, box_px_[2] - box_px_[0])
        f_, lines_, total_h_, sr_, ws_ = _fit_text_with_pixel_bounds(
            _dummy_draw, cased_headline,
            guidelines.typography.headline_font, fonts_dir,
            min_size_px=headline_min_px,
            max_size_px=headline_max_px,
            box_w=bw_,
            target_h=headline_target_h,
            preferred_line_count=preferred_lines,
            min_line_count=per_aspect.min_line_count,
            max_line_count=per_aspect.max_line_count,
            line_spacing=1.16,
            hero_scale_threshold_px=layout.headline_hero_scale_threshold_px,
        )
        max_lw_ = _max_line_width(_dummy_draw, lines_, f_)
        rb_px_ = _rendered_text_bbox_px(box_px_, max_lw_, total_h_, per_aspect.text_align)
        if total_h_ <= headline_target_h:
            fit_ = "fits"
        elif total_h_ <= headline_target_h + 4:
            fit_ = "near_fit"
        else:
            fit_ = "overflow"
        return {
            "box_pct": box_pct_in,
            "box_px": box_px_,
            "font": f_,
            "lines": lines_,
            "total_h": total_h_,
            "max_line_w": max_lw_,
            "rendered_bbox_px": rb_px_,
            "rendered_bbox_pct": _bbox_to_pct(rb_px_, target_w, target_h),
            "scale_reason": sr_,
            "fit_status": fit_,
            "wrap_strategy": ws_,
        }

    initial_fit = _fit_in_box(selected_box_pct)
    headline_box_original_pct = list(selected_box_pct)

    # Object-clearance check on the *rendered* text bbox (not the candidate
    # box). The candidate scoring already rejected obvious collisions, but
    # the rendered text may still crowd the focal area — especially when the
    # text wraps wider than expected.
    safe_zone_pct = focal_meta.get("product_safe_zone_box")
    if safe_zone_pct is not None:
        safe_zone_pct = tuple(safe_zone_pct)
    min_gap_px = focal_meta.get("min_gap_px_threshold") or 0
    initial_clearance = _clearance_metrics(
        initial_fit["rendered_bbox_pct"], safe_zone_pct,
        target_w, target_h, min_gap_px,
    )

    # Multi-attempt shift cascade. If the rendered bbox collides with or
    # near-misses the focal safe zone, try a deterministic series of
    # shifts (left/right, up/down, narrow toward the focal side). Accept
    # the first shift whose rendered bbox achieves clearance pass without
    # making the headline overflow its target.
    shift_attempts: list[dict] = []
    final_fit = initial_fit
    final_clearance = initial_clearance
    final_box_pct = selected_box_pct
    shift_success = bool(initial_clearance["clearance_pass"])
    shift_chosen_name: str | None = None

    needs_shift = (
        safe_zone_pct is not None
        and (initial_clearance["collision"] or initial_clearance["near_miss"])
    )
    if needs_shift:
        candidates_for_shift = _build_shift_candidates(
            selected_box_pct, safe_zone_pct, target_w, target_h, min_gap_px,
        )
        for name, candidate_box in candidates_for_shift:
            attempt_fit = _fit_in_box(candidate_box)
            attempt_clearance = _clearance_metrics(
                attempt_fit["rendered_bbox_pct"], safe_zone_pct,
                target_w, target_h, min_gap_px,
            )
            attempt_record = {
                "shift_name": name,
                "box_pct": [round(c, 4) for c in candidate_box],
                "rendered_bbox_px": list(attempt_fit["rendered_bbox_px"]),
                "gap_px": attempt_clearance["gap_px"],
                "clearance_pass": attempt_clearance["clearance_pass"],
                "fit_status": attempt_fit["fit_status"],
                "headline_size_px": getattr(attempt_fit["font"], "size", None),
                "accepted": False,
            }
            shift_attempts.append(attempt_record)
            # First-pass accept: clearance + good fit (preferred).
            if (
                attempt_clearance["clearance_pass"]
                and attempt_fit["fit_status"] in ("fits", "near_fit")
            ):
                attempt_record["accepted"] = True
                final_fit = attempt_fit
                final_clearance = attempt_clearance
                final_box_pct = candidate_box
                shift_success = True
                shift_chosen_name = name
                break

        # Second-pass: when no fits/near_fit shift cleared the focal area,
        # accept the best overflow shift that does. Better to render text
        # taller than its target zone than to crowd the focal product.
        if not shift_success:
            overflow_winners = [
                (idx, attempt_record)
                for idx, attempt_record in enumerate(shift_attempts)
                if attempt_record["clearance_pass"]
                and attempt_record["fit_status"] == "overflow"
            ]
            if overflow_winners:
                # Pick the one with the largest gap_px (most clearance).
                idx, _ = max(
                    overflow_winners,
                    key=lambda x: x[1]["gap_px"] or 0,
                )
                # Re-run the fit so we have the font/lines objects in hand.
                accepted_box = candidates_for_shift[idx][1]
                attempt_fit = _fit_in_box(accepted_box)
                attempt_clearance = _clearance_metrics(
                    attempt_fit["rendered_bbox_pct"], safe_zone_pct,
                    target_w, target_h, min_gap_px,
                )
                shift_attempts[idx]["accepted"] = True
                shift_attempts[idx]["accepted_overflow_fallback"] = True
                final_fit = attempt_fit
                final_clearance = attempt_clearance
                final_box_pct = accepted_box
                shift_success = True
                shift_chosen_name = (
                    f"{candidates_for_shift[idx][0]}_overflow_fallback"
                )

    # Update derived box state from the final fit (whether or not a shift won).
    selected_box_pct = final_box_pct
    bx0, by0, bx1, by1 = selected_box_pct
    hb = (
        int(bx0 * target_w), int(by0 * target_h),
        int(bx1 * target_w), int(by1 * target_h),
    )
    box_w = hb[2] - hb[0]
    box_h = hb[3] - hb[1]
    headline_font = final_fit["font"]
    headline_lines = final_fit["lines"]
    headline_total_h = final_fit["total_h"]
    headline_scale_reason = final_fit["scale_reason"]
    headline_fit_status = final_fit["fit_status"]
    rendered_bbox_px = final_fit["rendered_bbox_px"]
    rendered_bbox_pct = final_fit["rendered_bbox_pct"]

    # Surface the rendered text bbox to _choose_text_treatment so background
    # sampling matches what the type actually covers (not the wider box).
    text_render_box = rendered_bbox_px

    # 1) Contrast-aware overlay + text-color selection. Reads brand.qc as
    #    guidance only (no pass/fail decision here). QCCheckerAgent is the
    #    independent source of truth on the saved PNG. Sample over the
    #    rendered-text area so the composer's estimate aligns with QC's.
    canvas, text_hex, final_overlay_opacity, composer_contrast = _choose_text_treatment(
        cropped, text_render_box, layout, guidelines,
    )
    text_rgb = _hex_to_rgb(text_hex) + (255,)
    draw = ImageDraw.Draw(canvas)

    # Post-render color refinement: re-sample the bg under the rendered text
    # bbox (now that the overlay is painted) and pick whichever brand text
    # color yields the higher contrast. Records every candidate's contrast
    # so the report can show the selection rationale.
    headline_color_candidates_meta: list[dict] = []
    refined_bg_rgb = _box_mean_rgb_ext(canvas, rendered_bbox_px)
    for color_hex in (
        guidelines.typography.text_color_on_dark,
        guidelines.typography.text_color_on_light,
    ):
        ratio_local = _contrast.contrast_ratio(_contrast.hex_to_rgb(color_hex), refined_bg_rgb)
        headline_color_candidates_meta.append({
            "color": color_hex,
            "contrast": round(ratio_local, 2),
        })
    headline_color_candidates_meta.sort(key=lambda c: c["contrast"], reverse=True)
    refined_color_hex = headline_color_candidates_meta[0]["color"]
    headline_color_selection_reason = (
        f"sampled rendered-text bbox; picked {refined_color_hex} "
        f"({headline_color_candidates_meta[0]['contrast']}:1) over "
        f"{headline_color_candidates_meta[1]['color']} "
        f"({headline_color_candidates_meta[1]['contrast']}:1)"
        if len(headline_color_candidates_meta) > 1 else
        f"single candidate {refined_color_hex}"
    )
    if refined_color_hex != text_hex:
        text_hex = refined_color_hex
        text_rgb = _hex_to_rgb(text_hex) + (255,)

    # Vertical anchor: top of headline = box top (headline grows downward).
    headline_top = hb[1]
    headline_bottom = headline_top + headline_total_h

    # Two paths into the canvas behind the headline:
    #
    # (a) Explicit treatment (e.g. soft_panel always-on): paint it
    #     unconditionally and report it.
    # (b) Treatment is "none" (default for premium_product_hero): rely on the
    #     photography for natural negative space + brand text-shadow, and only
    #     escalate through readability_fallback_order if the contrast estimate
    #     falls below the brand threshold. Most renders end up at
    #     "natural_composition" with no visible panel.
    headline_panel_box: list[int] | None = None
    headline_panel_color_hex: str | None = None
    text_render_box_px = (hb[0], headline_top, hb[2], headline_bottom)
    max_line_w = _max_line_width(draw, headline_lines, headline_font)
    text_extent_x0, text_extent_x1 = _line_aligned_panel_x(
        hb[0], hb[2], max_line_w, per_aspect.text_align,
    )
    text_aligned_box = (text_extent_x0, headline_top, text_extent_x1, headline_bottom)

    if layout.headline_background_treatment == HeadlineBackgroundTreatment.SOFT_PANEL:
        # (a) explicit-panel path — always render.
        pad_px = max(1, int(min(target_w, target_h) * layout.headline_panel_padding_pct))
        panel_box = (
            max(0, text_extent_x0 - pad_px),
            max(0, headline_top - pad_px),
            min(target_w, text_extent_x1 + pad_px),
            min(target_h, headline_bottom + pad_px),
        )
        radius_px = max(0, int(min(target_w, target_h) * layout.headline_panel_corner_radius_pct))
        panel_color = _opposite_panel_color(text_rgb[:3])
        canvas = _render_translucent_panel(
            canvas, panel_box, panel_color, layout.headline_panel_opacity_pct, radius_px,
        )
        draw = ImageDraw.Draw(canvas)
        headline_panel_box = list(panel_box)
        headline_panel_color_hex = "#{:02X}{:02X}{:02X}".format(*panel_color)
        readability_fallback_used = "explicit_soft_panel"
        post_treatment_contrast = _estimate_text_contrast(canvas, text_aligned_box, text_rgb[:3])
    else:
        # (b) natural-composition path — fallback chain only fires if needed.
        canvas, readability_fallback_used, post_treatment_contrast = _apply_readability_fallback(
            canvas,
            text_box=text_aligned_box,
            headline_box=hb,
            text_align=per_aspect.text_align,
            text_rgb=text_rgb[:3],
            layout=layout,
            brand=guidelines,
        )
        draw = ImageDraw.Draw(canvas)
        # If the chain ended at the panel-last-resort, fill panel meta for
        # the report so the audit trail is complete.
        if readability_fallback_used == "soft_panel_last_resort":
            pad_px = max(1, int(min(target_w, target_h) * layout.headline_panel_padding_pct))
            headline_panel_box = [
                max(0, text_extent_x0 - pad_px),
                max(0, headline_top - pad_px),
                min(target_w, text_extent_x1 + pad_px),
                min(target_h, headline_bottom + pad_px),
            ]
            panel_color = _opposite_panel_color(text_rgb[:3])
            headline_panel_color_hex = "#{:02X}{:02X}{:02X}".format(*panel_color)

    # Render headline lines (with optional brand text-shadow on top of the panel).
    lh = _line_height(draw, headline_font)
    line_step = int(lh * 1.16)
    y = headline_top
    for line in headline_lines:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        x = _align_x(hb[0], hb[2], line_w, per_aspect.text_align)
        _draw_text_with_optional_shadow(
            draw, (x, y), line, headline_font, text_rgb, layout.headline_text_shadow,
        )
        y += line_step

    # Brand accent (rendered after headline so it can frame the headline box).
    canvas, accent_meta = _render_accent(
        canvas, layout, guidelines,
        text_box_px=hb,
        headline_top_px=headline_top,
        headline_bottom_px=headline_bottom,
        ratio=ratio,
    )

    # Disclaimer. Use the new candidate-box scorer when configured;
    # otherwise fall back to the legacy single-zone placement.
    disclaimer_box_meta: list[int] | None = None
    disclaimer_placement_box_meta: list[int] | None = None
    disclaimer_badge_box: list[int] | None = None
    disclaimer_badge_color_hex: str | None = None
    disclaimer_selection_meta: dict | None = None
    d_font = None
    if disclaimer:
        legacy_db = _disclaimer_box(
            target_w, target_h, hb, headline_bottom,
            layout.disclaimer_placement, layout.disclaimer_padding_pct,
        )
        disclaimer_selection_meta = select_disclaimer_box(
            canvas, layout, ratio,
            focal_safe_zone_pct=safe_zone_pct,
            text_color_options=[
                guidelines.typography.text_color_on_dark,
                guidelines.typography.text_color_on_light,
            ],
            fallback_box_px=legacy_db,
        )
        db = disclaimer_selection_meta["box_px"]
        d_w = max(1, db[2] - db[0])
        d_h = max(1, db[3] - db[1])

        if typo_per_aspect is not None:
            d_min_px = typo_per_aspect.disclaimer_min_font_size_px
            d_max_px = typo_per_aspect.disclaimer_max_font_size_px
            d_size_pct = typo_per_aspect.disclaimer_font_size_pct_of_height
            # Initial target derives from per-aspect pct (when set), else
            # the global typography.body_size_ratio. Clamped to [min, max].
            initial_pct = d_size_pct if d_size_pct is not None else body_ratio
            d_initial_target = int(target_h * initial_pct)
            d_max_for_fit = max(d_min_px, min(d_max_px, d_initial_target))
        else:
            d_min_px = _FALLBACKS["min_font_size"]
            d_max_for_fit = max(d_min_px, int(target_h * body_ratio))

        draw_d = ImageDraw.Draw(canvas)
        d_font, d_lines, d_total_h, _, _ = _fit_text_with_pixel_bounds(
            draw_d, disclaimer,
            guidelines.typography.body_font, fonts_dir,
            min_size_px=d_min_px,
            max_size_px=d_max_for_fit,
            box_w=d_w,
            target_h=d_h,
            preferred_line_count=None,
            line_spacing=1.18,
        )
        d_y_start = db[1]
        d_lh = _line_height(draw_d, d_font)
        d_step = int(d_lh * 1.18)
        d_align = (
            per_aspect.text_align
            if layout.disclaimer_placement == DisclaimerPlacement.UNDER_HEADLINE
            else (TextAlign.RIGHT if layout.disclaimer_placement == DisclaimerPlacement.BOTTOM_CORNER else TextAlign.CENTER)
        )

        # Soft local readability badge behind the disclaimer (paint before text).
        if layout.disclaimer_background_treatment == DisclaimerBackgroundTreatment.SOFT_BADGE:
            d_max_lw = _max_line_width(draw_d, d_lines, d_font)
            d_total_h_for_badge = d_step * len(d_lines)
            badge_text_x0, badge_text_x1 = _line_aligned_panel_x(
                db[0], db[2], d_max_lw, d_align,
            )
            badge_pad = max(1, int(min(target_w, target_h) * layout.disclaimer_badge_padding_pct))
            badge_box = (
                max(0, badge_text_x0 - badge_pad),
                max(0, db[1] - badge_pad),
                min(target_w, badge_text_x1 + badge_pad),
                min(target_h, db[1] + d_total_h_for_badge + badge_pad),
            )
            badge_radius = max(0, int(min(target_w, target_h) * layout.disclaimer_badge_corner_radius_pct))
            badge_color = _opposite_panel_color(text_rgb[:3])
            canvas = _render_translucent_panel(
                canvas, badge_box, badge_color, layout.disclaimer_badge_opacity_pct, badge_radius,
            )
            draw_d = ImageDraw.Draw(canvas)
            disclaimer_badge_box = list(badge_box)
            disclaimer_badge_color_hex = "#{:02X}{:02X}{:02X}".format(*badge_color)

        # Compute the actual rendered-text bbox so QC samples under the
        # badge (not the surrounding empty placement region).
        d_max_lw = _max_line_width(draw_d, d_lines, d_font)
        d_text_x0, d_text_x1 = _line_aligned_panel_x(db[0], db[2], d_max_lw, d_align)
        d_total_render_h = d_step * len(d_lines)

        # Pick disclaimer text color independently from the headline's
        # choice — the disclaimer sits in a different region (bottom corner)
        # whose background can differ substantially from the headline area.
        # Without this, moving the headline (e.g. text-zone scoring picks
        # an upper-left box) would leave the disclaimer with a stale color.
        disclaimer_text_box = (d_text_x0, db[1], d_text_x1, db[1] + d_total_render_h)
        disclaimer_bg = _box_mean_rgb_ext(canvas, disclaimer_text_box)
        d_color_options = [
            guidelines.typography.text_color_on_dark,
            guidelines.typography.text_color_on_light,
        ]
        d_scored = sorted(
            (
                (c, _contrast.contrast_ratio(_contrast.hex_to_rgb(c), disclaimer_bg))
                for c in d_color_options
            ),
            key=lambda x: x[1],
            reverse=True,
        )
        disclaimer_text_hex = d_scored[0][0]
        disclaimer_text_rgb = _hex_to_rgb(disclaimer_text_hex) + (255,)

        for line in d_lines:
            bbox = draw_d.textbbox((0, 0), line, font=d_font)
            lw = bbox[2] - bbox[0]
            x = _align_x(db[0], db[2], lw, d_align)
            draw_d.text((x, d_y_start), line, font=d_font, fill=disclaimer_text_rgb)
            d_y_start += d_step
        # Report the rendered-text bbox (where QC should sample). Keep the
        # original placement region under disclaimer_placement_box for
        # telemetry.
        disclaimer_box_meta = [d_text_x0, db[1], d_text_x1, db[1] + d_total_render_h]
        disclaimer_placement_box_meta = [db[0], db[1], db[2], db[3]]

    # 5) logo — pre-compute footprint, then iterate
    #    ``visual_identity.logo_allowed_positions`` until one clears the
    #    focal safe zone. Falls back to the configured corner with a
    #    selection_reason warning when none of the alternates work.
    logo_w_for_pick, logo_h_for_pick = _compute_logo_footprint(
        target_w, target_h, guidelines,
    )
    logo_pick = select_logo_position(
        target_w, target_h, logo_w_for_pick, logo_h_for_pick,
        guidelines, ratio, focal_safe_zone_pct=safe_zone_pct,
    )
    canvas, logo_meta = stamp_logo(
        canvas, guidelines, placement_override=logo_pick["placement"],
    )

    # 6) save
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(out_path, "PNG")
    duration_ms = int((time.monotonic() - started) * 1000)

    accent_color_hex = _resolve_role_color(layout.accent_color_role, guidelines)
    return {
        "path": out_path,
        "ratio": ratio,
        "size": (target_w, target_h),
        "duration_ms": duration_ms,
        "disclaimer_rendered": disclaimer is not None,
        # Text color the composer actually used — drives WCAG contrast QC.
        "text_color_used": text_hex,
        # Layout accounting for the report.
        "headline_box": [hb[0], headline_top, hb[2], headline_bottom],
        "disclaimer_box": disclaimer_box_meta,
        "logo_box": (
            None if logo_meta["position"] is None
            else [logo_meta["position"][0], logo_meta["position"][1],
                  logo_meta["position"][0] + logo_meta["size_px"][0],
                  logo_meta["position"][1] + logo_meta["size_px"][1]]
        ),
        "logo_position": logo_meta["position"],
        "logo_size_px": logo_meta["size_px"],
        "logo_treatment": logo_meta["treatment"],
        "overlay_style": layout.overlay_style.value,
        # Actual opacity used after any contrast-driven escalation
        # (may differ from layout.overlay_opacity() when the composer bumped it).
        "overlay_opacity": final_overlay_opacity,
        "overlay_opacity_configured": layout.overlay_opacity(),
        "composer_contrast_estimate": round(composer_contrast, 2),
        # Final rendered type sizes (pixels). headline_size_px is the chosen
        # size from per-aspect bounds; disclaimer_size_px is None when no
        # disclaimer was rendered.
        "headline_size_px": getattr(headline_font, "size", None),
        "disclaimer_size_px": (
            getattr(d_font, "size", None) if disclaimer else None
        ),
        "headline_line_count": len(headline_lines),
        "accent_style": layout.accent_style.value,
        "accent_color": accent_color_hex if layout.accent_style != AccentStyle.NONE else None,
        # Local readability treatments (panel/badge).
        "headline_background_treatment": layout.headline_background_treatment.value,
        "headline_panel_box": headline_panel_box,
        "headline_panel_color": headline_panel_color_hex,
        "headline_text_shadow": layout.headline_text_shadow,
        # Which readability strategy ended up being applied:
        #   "natural_composition"        — no panel/gradient needed
        #   "subtle_text_shadow"         — perceptual bump from configured shadow
        #   "subtle_local_gradient"      — feathered local gradient applied
        #   "soft_panel_last_resort"     — fell back to an opaque panel
        #   "explicit_soft_panel"        — layout configured panel always-on
        #   "exhausted_fallbacks"        — chain ran out without meeting threshold
        "readability_fallback_used": readability_fallback_used,
        "post_treatment_contrast_estimate": round(post_treatment_contrast, 2),
        # Whether the brand provided composition guidance at all (gives the
        # report a single boolean to flag "this run benefited from
        # photographic negative-space direction").
        "image_composition_guidance_used": (
            guidelines.image_composition_guidance is not None
            and guidelines.image_composition_guidance.negative_space_required
        ),
        "negative_space_location": (
            (guidelines.image_composition_guidance.negative_space_location_by_aspect.get(ratio)
             if guidelines.image_composition_guidance else None)
        ),
        # Headline-zone selection audit. When candidate scoring fired, the
        # winner's metrics + the full per-candidate breakdown are recorded
        # so reviewers can see why this zone was chosen.
        "headline_box_selected_pct": list(selected_box_pct),
        "headline_box_selected_px": [hb[0], hb[1], hb[2], hb[3]],
        "headline_box_selection_reason": headline_box_selection["selection_reason"],
        "headline_box_score": headline_box_selection["score"],
        "headline_region_texture_score": headline_box_selection["texture_score"],
        "headline_region_edge_density": headline_box_selection["edge_density"],
        "headline_region_contrast_estimate": headline_box_selection["contrast_estimate"],
        "headline_box_candidates": all_candidate_scores,
        # Headline-box adjustment audit. ``headline_box_original`` is the
        # candidate box the scorer picked; ``headline_box_adjusted`` records
        # whether a shift won. ``headline_box_shift_attempts`` lists every
        # shift the cascade tried (including rejected ones) so reviewers can
        # audit why the final box was chosen.
        "headline_box_original": headline_box_original_pct,
        "headline_box_adjusted": (
            list(selected_box_pct)
            if list(selected_box_pct) != headline_box_original_pct else None
        ),
        "headline_box_adjustment_reason": (
            f"shift_cascade:{shift_chosen_name}" if shift_chosen_name
            else headline_box_selection.get("headline_box_adjustment_reason")
        ),
        "headline_box_shift_attempts": shift_attempts,
        "headline_box_shift_success": shift_success,
        # Color picked for the headline (alias of text_color_used; explicit
        # surface for the report's headline_color_selected field).
        "headline_color_selected": text_hex,
        "headline_color_candidates": headline_color_candidates_meta,
        "headline_color_selection_reason": headline_color_selection_reason,
        # Wrap/scale audit.
        "headline_wrap_variant": f"{len(headline_lines)}_lines_at_{getattr(headline_font, 'size', 0)}px",
        "headline_scale_reason": headline_scale_reason,
        "headline_fit_status": headline_fit_status,
        # Rendered text bbox in pixel coords — the *actual* text area on
        # canvas, accounting for line wrapping width and alignment. This is
        # the bbox QC and the prominence score sample under, not the full
        # candidate box.
        "rendered_headline_bbox": list(rendered_bbox_px),
        "rendered_headline_bbox_pct": [round(c, 4) for c in rendered_bbox_pct],
        # Prominence score: weighted combination of size-vs-ceiling, zone
        # fill, line count, fit, contrast, and clearance. >= 0.7 = confident
        # campaign hero; <= 0.5 = timid caption-sized render. Components are
        # surfaced separately so the report can audit which factor pulled it
        # down. See ``_compute_prominence_score`` for the formula.
        **_compute_prominence_score(
            font_size_px=getattr(headline_font, "size", 0),
            max_size_px=headline_max_px,
            min_size_px=headline_min_px,
            zone_fill_pct=headline_total_h / max(1, box_h),
            target_zone_fill_pct=(
                typo_per_aspect.headline_target_zone_fill_pct
                if typo_per_aspect is not None else 0.5
            ),
            line_count=len(headline_lines),
            preferred_lines=preferred_lines,
            min_lines=per_aspect.min_line_count,
            max_lines=per_aspect.max_line_count,
            fit_status=headline_fit_status,
            contrast=composer_contrast,
            clearance_pass=final_clearance["clearance_pass"],
            collision=final_clearance["collision"],
            near_miss=final_clearance["near_miss"],
        ),
        "headline_max_size_px_configured": headline_max_px,
        "headline_min_size_px_configured": headline_min_px,
        "headline_target_h_px": headline_target_h,
        "headline_target_zone_fill_pct": (
            typo_per_aspect.headline_target_zone_fill_pct
            if typo_per_aspect is not None else None
        ),
        "headline_font_size_px": getattr(headline_font, "size", None),
        # Focal-area + product-safe-zone audit. ``text_object_*`` fields now
        # reflect the *rendered text bbox* (not the candidate box), which is
        # the visually meaningful clearance.
        "focal_area_estimate": focal_meta["focal_area_estimate"],
        "product_safe_zone_box": focal_meta["product_safe_zone_box"],
        "expanded_product_safe_zone_box": focal_meta.get("expanded_product_safe_zone_box"),
        "focal_overlap_detected": focal_meta["focal_overlap_detected"],
        "focal_near_miss_detected": focal_meta.get("focal_near_miss_detected", False),
        "focal_overlap_pct": focal_meta["focal_overlap_pct"],
        "text_object_gap_px": final_clearance["gap_px"],
        "text_object_clearance_pass": final_clearance["clearance_pass"],
        "text_object_collision_detected": final_clearance["collision"],
        "text_object_near_miss_detected": final_clearance["near_miss"],
        "all_candidates_failed_clearance": focal_meta.get(
            "all_candidates_failed_clearance", False
        ),
        "clearance_failure_reason": (
            None if final_clearance["clearance_pass"]
            else (
                "rendered_text_bbox_collides_with_focal_safe_zone"
                if final_clearance["collision"]
                else f"rendered_text_bbox_within_{min_gap_px}px_of_focal_safe_zone"
                if final_clearance["near_miss"]
                else "unknown"
            )
        ),
        "min_text_object_gap_px_threshold": min_gap_px,
        # Disclaimer clearance audit — gap from disclaimer rendered text bbox
        # to the focal/product safe zone, and pass/fail flag.
        "disclaimer_text_object_gap_px": (
            None if (disclaimer_box_meta is None or focal_meta.get("product_safe_zone_box") is None)
            else round(_box_gap_px(
                (disclaimer_box_meta[0] / target_w, disclaimer_box_meta[1] / target_h,
                 disclaimer_box_meta[2] / target_w, disclaimer_box_meta[3] / target_h),
                tuple(focal_meta["product_safe_zone_box"]),
                target_w, target_h,
            ), 1)
        ),
        "disclaimer_clearance_pass": (
            True if (disclaimer_box_meta is None or focal_meta.get("product_safe_zone_box") is None)
            else _box_overlap_pct(
                (disclaimer_box_meta[0] / target_w, disclaimer_box_meta[1] / target_h,
                 disclaimer_box_meta[2] / target_w, disclaimer_box_meta[3] / target_h),
                tuple(focal_meta["product_safe_zone_box"]),
            ) == 0.0
        ),
        "disclaimer_background_treatment": layout.disclaimer_background_treatment.value,
        "disclaimer_badge_box": disclaimer_badge_box,
        "disclaimer_badge_color": disclaimer_badge_color_hex,
        "disclaimer_placement_box": disclaimer_placement_box_meta,
        # Disclaimer candidate-box selection audit (when configured).
        "disclaimer_candidate_index": (
            disclaimer_selection_meta.get("candidate_index")
            if disclaimer_selection_meta else None
        ),
        "disclaimer_candidate_attempts": (
            disclaimer_selection_meta.get("candidate_attempts")
            if disclaimer_selection_meta else None
        ),
        "disclaimer_position_selected": (
            f"candidate_{disclaimer_selection_meta['candidate_index']}"
            if disclaimer_selection_meta and disclaimer_selection_meta.get("candidate_index") is not None
            else layout.disclaimer_placement.value
        ),
        "disclaimer_position_configured": layout.disclaimer_placement.value,
        "disclaimer_selection_reason": (
            disclaimer_selection_meta.get("selection_reason")
            if disclaimer_selection_meta else None
        ),
        # Headline zone fill — telemetry, not a target. (rendered_h / box_h)
        "headline_zone_fill_pct": round(headline_total_h / max(1, box_h), 3),
        # Surface the disclaimer text color the composer actually used —
        # picked independently from the headline based on the disclaimer
        # area's bg, so QC and reviewers see the right color.
        "disclaimer_text_color": disclaimer_text_hex if disclaimer else None,
        # Crop-scoring audit (the new anti-clip stage).
        "crop_strategy_used": crop_meta.get("crop_strategy_used"),
        "crop_box_used": crop_meta.get("crop_box_used"),
        "crop_box_candidates": crop_meta.get("crop_box_candidates"),
        "crop_box_scores": crop_meta.get("crop_box_scores"),
        "focal_edge_gap_px": crop_meta.get("focal_edge_gap_px"),
        "focal_edge_min_gap_px": crop_meta.get("min_edge_gap_px"),
        "focal_edge_clearance_pass": crop_meta.get("focal_edge_clearance_pass"),
        "focal_edge_clip_detected": crop_meta.get("focal_edge_clip_detected"),
        "focal_edges_touched": crop_meta.get("edges_touched"),
        "crop_edge_clip_penalty_applied": crop_meta.get("crop_edge_clip_penalty_applied"),
        # Letterbox fallback audit (only set when the crop scorer ran out
        # of viable offsets and the layout had letterboxing enabled).
        "letterbox_applied": crop_meta.get("letterbox_applied", False),
        "letterbox_pad_pct": crop_meta.get("letterbox_pad_pct"),
        "letterbox_color_used": crop_meta.get("letterbox_color_used"),
        "letterbox_color_source": crop_meta.get("letterbox_color_source"),
        # Headline wrap strategy ("greedy" or "balanced") + adaptive
        # widening audit. Together these explain how the headline went
        # from a narrow candidate to its final hero-scale rendering.
        "headline_wrap_strategy": final_fit.get("wrap_strategy"),
        "headline_box_widened": headline_box_widened,
        "headline_box_width_delta_pct": round(widen_delta_pct, 4),
        "headline_box_pre_widen_pct": [round(c, 4) for c in pre_widen_box_pct],
        "headline_widen_reason": widen_reason,
        # Logo placement audit.
        "logo_position_selected": logo_pick.get("logo_position_selected"),
        "logo_position_configured": logo_pick.get("logo_position_configured"),
        "logo_position_adjusted": logo_pick.get("logo_position_adjusted"),
        "logo_product_gap_px": logo_pick.get("logo_product_gap_px"),
        "logo_product_clearance_pass": logo_pick.get("logo_product_clearance_pass"),
        "logo_collision_detected": logo_pick.get("logo_collision_detected"),
        "logo_selection_reason": logo_pick.get("logo_selection_reason"),
        "logo_position_attempts": logo_pick.get("logo_position_attempts"),
        "logo_min_required_gap_px": logo_pick.get("logo_min_required_gap_px"),
        # Accent safe-zone audit.
        "accent_line_box": accent_meta.get("accent_line_box"),
        "accent_edge_gap_px": accent_meta.get("accent_edge_gap_px"),
        "accent_safe_zone_pass": accent_meta.get("accent_safe_zone_pass"),
        # Composition score: weighted aggregate of crop / logo / headline /
        # accent / disclaimer health. Warnings name every issue still on
        # the canvas so reviewers can spot regressions at a glance.
        **_compute_composition_score(
            crop_clearance_pass=bool(crop_meta.get("focal_edge_clearance_pass")),
            crop_clip_detected=bool(crop_meta.get("focal_edge_clip_detected")),
            logo_clearance_pass=bool(logo_pick.get("logo_product_clearance_pass")),
            logo_collision=bool(logo_pick.get("logo_collision_detected")),
            headline_prominence=(
                getattr(headline_font, "size", 0) / max(1, headline_max_px)
            ),
            headline_clearance_pass=bool(final_clearance["clearance_pass"]),
            headline_too_small=(
                getattr(headline_font, "size", 0) <= headline_min_px
            ),
            accent_safe_zone_pass=bool(accent_meta.get("accent_safe_zone_pass", True)),
            disclaimer_clearance_pass=(
                bool(disclaimer_selection_meta.get("clearance_pass"))
                if disclaimer_selection_meta else True
            ),
            all_text_candidates_failed=bool(
                focal_meta.get("all_candidates_failed_clearance", False)
            ),
        ),
    }


# Keep the legacy entry points exported so older imports keep working.
def add_text_overlay(*args, **kwargs):  # pragma: no cover — kept for back-compat
    raise RuntimeError(
        "add_text_overlay() is deprecated; use compose_creative() which orchestrates "
        "the full overlay/headline/accent/logo pipeline."
    )
