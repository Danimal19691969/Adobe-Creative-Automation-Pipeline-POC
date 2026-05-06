"""Palette extraction + logo detection helpers used by BrandCheckerAgent."""

from __future__ import annotations

import logging
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def dominant_palette(image_path: str, n: int = 5) -> list[str]:
    """Return the top-n dominant colors as #RRGGBB hex strings."""
    img = Image.open(image_path).convert("RGB")
    quantized = img.quantize(colors=n, kmeans=n)
    palette = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    out: list[str] = []
    for _count, idx in counts[:n]:
        r = palette[idx * 3]
        g = palette[idx * 3 + 1]
        b = palette[idx * 3 + 2]
        out.append(f"#{r:02X}{g:02X}{b:02X}")
    return out


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _euclid(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def palette_distance(palette: list[str], brand_colors: list[str]) -> float:
    """Mean nearest-brand-color distance across the extracted palette.
    0 = perfect match; ~441 = max possible (black vs white)."""
    if not palette or not brand_colors:
        return float("inf")
    brand_rgb = [_hex_to_rgb(c) for c in brand_colors]
    distances = []
    for hex_color in palette:
        c = _hex_to_rgb(hex_color)
        distances.append(min(_euclid(c, b) for b in brand_rgb))
    return sum(distances) / len(distances)


_PLACEMENT_TO_QUADRANT = {
    "top-left":     lambda x, y, W, H: x < W / 2 and y < H / 2,
    "top-right":    lambda x, y, W, H: x >= W / 2 and y < H / 2,
    "bottom-left":  lambda x, y, W, H: x < W / 2 and y >= H / 2,
    "bottom-right": lambda x, y, W, H: x >= W / 2 and y >= H / 2,
}


def detect_logo(image_path: str, logo_path: str, expected_placement: str) -> dict:
    """Locate the logo in the image via cv2.matchTemplate.
    Returns {found, position, match_score, placement_ok}."""
    if not Path(logo_path).exists():
        return {"found": False, "match_score": 0.0, "position": None, "placement_ok": False, "reason": "logo file missing"}

    haystack = cv2.imread(image_path, cv2.IMREAD_COLOR)
    needle_rgba = cv2.imread(logo_path, cv2.IMREAD_UNCHANGED)
    if haystack is None or needle_rgba is None:
        return {"found": False, "match_score": 0.0, "position": None, "placement_ok": False, "reason": "image read failed"}

    has_alpha = needle_rgba.ndim == 3 and needle_rgba.shape[2] == 4
    if has_alpha:
        needle_bgr = cv2.cvtColor(needle_rgba, cv2.COLOR_BGRA2BGR)
        alpha = needle_rgba[:, :, 3]
    else:
        needle_bgr = needle_rgba
        alpha = None

    H, W = haystack.shape[:2]
    # Logo in the haystack is approximately 10% of min canvas dim, see
    # pillow_composer._LOGO_HEIGHT_PCT. Scale needle to that target before matching.
    target_h = int(min(W, H) * 0.10)
    aspect = needle_bgr.shape[1] / needle_bgr.shape[0]
    target_w = max(1, int(target_h * aspect))
    needle_scaled = cv2.resize(needle_bgr, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

    if alpha is not None:
        mask_scaled = cv2.resize(alpha, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)
        # cv2's TM_CCORR_NORMED supports a per-pixel mask for transparent-background
        # template matching. Convert alpha to a 3-channel mask.
        mask3 = cv2.merge([mask_scaled, mask_scaled, mask_scaled])
        result = cv2.matchTemplate(haystack, needle_scaled, cv2.TM_CCORR_NORMED, mask=mask3)
    else:
        result = cv2.matchTemplate(haystack, needle_scaled, cv2.TM_CCOEFF_NORMED)

    # Mask-based scores can be NaN/Inf in fully-masked regions; sanitize.
    result = np.nan_to_num(result, nan=0.0, posinf=0.0, neginf=0.0)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    cx = max_loc[0] + target_w / 2
    cy = max_loc[1] + target_h / 2

    placement_check = _PLACEMENT_TO_QUADRANT.get(expected_placement, lambda *_: False)
    placement_ok = placement_check(cx, cy, W, H)
    found = bool(max_val > 0.7)

    return {
        "found": found,
        "match_score": float(max_val),
        "position": [int(max_loc[0]), int(max_loc[1])],
        "size": [target_w, target_h],
        "placement_ok": placement_ok if found else False,
    }
