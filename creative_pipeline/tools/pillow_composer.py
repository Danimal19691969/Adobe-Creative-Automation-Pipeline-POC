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
    clearance_ok = not hard_collision if hard_fail_collision else True
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
                "focal_density": focal_density,
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
            max_texture_norm=layout.text_region_max_texture_score,
            max_edge_norm=layout.text_region_max_edge_density,
        )
        for box_pct in candidates
    ]
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
            "focal_density": focal_density,
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

    candidates: list[tuple[int, ImageFont.ImageFont, list[str], int]] = []
    # Step by 2 px for a tight resolution without doing 90 wrapping passes.
    for size in range(max_size_px, min_size_px - 1, -2):
        font = _load_font(fonts_dir, font_filename, size)
        lines = _wrap_text(draw, text, font, box_w)
        lh = _line_height(draw, font)
        total_h = int(lh * line_spacing) * len(lines)
        if total_h <= target_h:
            candidates.append((size, font, lines, total_h))

    if not candidates:
        font = _load_font(fonts_dir, font_filename, min_size_px)
        lines = _wrap_text(draw, text, font, box_w)
        lh = _line_height(draw, font)
        return (
            font, lines, int(lh * line_spacing) * len(lines),
            f"min_floor_overflow (no size in [{min_size_px}, {max_size_px}] fit target_h={target_h})",
        )

    def _within_bounds(n: int) -> bool:
        if min_line_count is not None and n < min_line_count:
            return False
        if max_line_count is not None and n > max_line_count:
            return False
        return True

    # 1. Preferred line count, if achievable AND in bounds.
    if preferred_line_count is not None and _within_bounds(preferred_line_count):
        preferred = [c for c in candidates if len(c[2]) == preferred_line_count]
        if preferred:
            size, font, lines, total_h = preferred[0]
            return font, lines, total_h, (
                f"preferred_line_count={preferred_line_count} at largest fitting size ({size}px)"
            )

    # 2. Within line-count bounds.
    if min_line_count is not None or max_line_count is not None:
        bounded = [c for c in candidates if _within_bounds(len(c[2]))]
        if bounded:
            size, font, lines, total_h = bounded[0]
            return font, lines, total_h, (
                f"largest size honoring line bounds [{min_line_count}, {max_line_count}] ({size}px, {len(lines)} lines)"
            )

    # 3. Largest fitting size, line count whatever it is.
    size, font, lines, total_h = candidates[0]
    return font, lines, total_h, (
        f"largest fitting size ({size}px, {len(lines)} lines)"
    )


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
    src_w, src_h = img.size
    target_aspect = target_w / target_h
    src_aspect = src_w / src_h
    if abs(src_aspect - target_aspect) < 0.01:
        return img.resize((target_w, target_h), Image.LANCZOS)
    if src_aspect > target_aspect:
        new_w = int(src_h * target_aspect)
        x0 = (src_w - new_w) // 2
        cropped = img.crop((x0, 0, x0 + new_w, src_h))
    else:
        new_h = int(src_w / target_aspect)
        max_y0 = src_h - new_h
        # Bias upward so the product hero (typically upper third in our prompt)
        # stays above the headline overlay band.
        y0 = min(max_y0, max(0, int(max_y0 * 0.20)))
        cropped = img.crop((0, y0, src_w, y0 + new_h))
    return cropped.resize((target_w, target_h), Image.LANCZOS)


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


def _render_accent(
    canvas: Image.Image,
    layout: LayoutTemplate,
    brand: BrandGuidelines,
    text_box_px: tuple[int, int, int, int],
    headline_top_px: int,
    headline_bottom_px: int,
) -> Image.Image:
    if layout.accent_style == AccentStyle.NONE:
        return canvas

    W, H = canvas.size
    color_hex = _resolve_role_color(layout.accent_color_role, brand)
    color_rgba = _hex_to_rgb(color_hex) + (255,)
    thickness = max(2, int(min(W, H) * layout.accent_thickness_pct))

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x0, y0, x1, y1 = text_box_px

    if layout.accent_style == AccentStyle.SIDE_RAIL:
        # Vertical rail flush with the left of the headline box, spanning the
        # vertical extent of the rendered headline.
        draw.rectangle(
            (x0 - thickness * 2, headline_top_px, x0 - thickness, headline_bottom_px),
            fill=color_rgba,
        )
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

    return Image.alpha_composite(canvas, overlay)


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


def stamp_logo(
    img: Image.Image,
    brand: BrandGuidelines,
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
        pos = _logo_position(W, H, badge_w, badge_h, vi.logo_placement, safe_x, safe_y)
        canvas.alpha_composite(badge, dest=pos)
        logo_w, logo_h = badge_w, badge_h
    else:
        # Plain treatment: single LANCZOS resize of the source logo.
        logo = logo_source.resize((target_w, target_h), Image.LANCZOS)
        pos = _logo_position(W, H, target_w, target_h, vi.logo_placement, safe_x, safe_y)
        canvas.alpha_composite(logo, dest=pos)
        logo_w, logo_h = target_w, target_h

    return canvas, {
        "position": list(pos),
        "size_px": [logo_w, logo_h],
        "treatment": vi.logo_treatment.value,
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
    cropped = smart_crop(hero, target_w, target_h)

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
    headline_font, headline_lines, headline_total_h, headline_scale_reason = _fit_text_with_pixel_bounds(
        ImageDraw.Draw(_dummy), cased_headline,
        guidelines.typography.headline_font, fonts_dir,
        min_size_px=headline_min_px,
        max_size_px=headline_max_px,
        box_w=box_w,
        target_h=headline_target_h,
        preferred_line_count=preferred_lines,
        min_line_count=per_aspect.min_line_count,
        max_line_count=per_aspect.max_line_count,
        line_spacing=1.16,
    )
    # "fits" when the rendered text height respects the brand's target zone
    # fill cap; "near_fit" when it overflows by under 4 px (rounding); else
    # "overflow" — the brand's min-font-size floor was binding.
    if headline_total_h <= headline_target_h:
        headline_fit_status = "fits"
    elif headline_total_h <= headline_target_h + 4:
        headline_fit_status = "near_fit"
    else:
        headline_fit_status = "overflow"
    text_render_box = (hb[0], hb[1], hb[2], hb[1] + headline_total_h)

    # 1) Contrast-aware overlay + text-color selection. Reads brand.qc as
    #    guidance only (no pass/fail decision here). QCCheckerAgent is the
    #    independent source of truth on the saved PNG. Sample over the
    #    rendered-text area so the composer's estimate aligns with QC's.
    canvas, text_hex, final_overlay_opacity, composer_contrast = _choose_text_treatment(
        cropped, text_render_box, layout, guidelines,
    )
    text_rgb = _hex_to_rgb(text_hex) + (255,)
    draw = ImageDraw.Draw(canvas)

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
    canvas = _render_accent(
        canvas, layout, guidelines,
        text_box_px=hb,
        headline_top_px=headline_top,
        headline_bottom_px=headline_bottom,
    )

    # Disclaimer.
    disclaimer_box_meta: list[int] | None = None
    disclaimer_placement_box_meta: list[int] | None = None
    disclaimer_badge_box: list[int] | None = None
    disclaimer_badge_color_hex: str | None = None
    d_font = None
    if disclaimer:
        db = _disclaimer_box(
            target_w, target_h, hb, headline_bottom,
            layout.disclaimer_placement, layout.disclaimer_padding_pct,
        )
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
        d_font, d_lines, d_total_h, _ = _fit_text_with_pixel_bounds(
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

    # 5) logo
    canvas, logo_meta = stamp_logo(canvas, guidelines)

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
        # Headline-box adjustment audit (post-selection shrink/shift to
        # widen the gap to the focal area when near-miss is detected).
        "headline_box_original": headline_box_selection.get("headline_box_original"),
        "headline_box_adjusted": headline_box_selection.get("headline_box_adjusted"),
        "headline_box_adjustment_reason": headline_box_selection.get("headline_box_adjustment_reason"),
        # Color picked for the headline (alias of text_color_used; explicit
        # surface for the report's headline_color_selected field).
        "headline_color_selected": text_hex,
        # Wrap/scale audit.
        "headline_wrap_variant": f"{len(headline_lines)}_lines_at_{getattr(headline_font, 'size', 0)}px",
        "headline_scale_reason": headline_scale_reason,
        "headline_fit_status": headline_fit_status,
        # Prominence score: how confidently the composer used its configured
        # ceiling. 1.0 = chose the brand's headline_max; ~0.5 = stayed near
        # the floor. Lets a reviewer spot timid renders at a glance.
        "headline_prominence_score": round(
            getattr(headline_font, "size", 0) / max(1, headline_max_px), 3
        ),
        "headline_max_size_px_configured": headline_max_px,
        "headline_min_size_px_configured": headline_min_px,
        "headline_target_h_px": headline_target_h,
        # Focal-area + product-safe-zone audit.
        "focal_area_estimate": focal_meta["focal_area_estimate"],
        "product_safe_zone_box": focal_meta["product_safe_zone_box"],
        "expanded_product_safe_zone_box": focal_meta.get("expanded_product_safe_zone_box"),
        "focal_overlap_detected": focal_meta["focal_overlap_detected"],
        "focal_near_miss_detected": focal_meta.get("focal_near_miss_detected", False),
        "focal_overlap_pct": focal_meta["focal_overlap_pct"],
        "text_object_gap_px": focal_meta.get("text_object_gap_px"),
        "text_object_clearance_pass": focal_meta.get("text_object_clearance_pass"),
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
        # Headline zone fill — telemetry, not a target. (rendered_h / box_h)
        "headline_zone_fill_pct": round(headline_total_h / max(1, box_h), 3),
        # Surface the disclaimer text color the composer actually used —
        # picked independently from the headline based on the disclaimer
        # area's bg, so QC and reviewers see the right color.
        "disclaimer_text_color": disclaimer_text_hex if disclaimer else None,
    }


# Keep the legacy entry points exported so older imports keep working.
def add_text_overlay(*args, **kwargs):  # pragma: no cover — kept for back-compat
    raise RuntimeError(
        "add_text_overlay() is deprecated; use compose_creative() which orchestrates "
        "the full overlay/headline/accent/logo pipeline."
    )
