"""WCAG 2.x contrast math + background-color estimation.

Pure functions, no agent dependencies. Used by ``tools.qc_rules.ContrastRule``
and any caller that needs a contrast check.

References:
  https://www.w3.org/TR/WCAG21/#contrast-minimum
  https://www.w3.org/TR/WCAG21/#dfn-relative-luminance
"""

from __future__ import annotations

from PIL import Image


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.x relative luminance (0.0 = black, 1.0 = white)."""
    def to_linear(c: int) -> float:
        cs = c / 255.0
        return cs / 12.92 if cs <= 0.03928 else ((cs + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * to_linear(r) + 0.7152 * to_linear(g) + 0.0722 * to_linear(b)


def contrast_ratio(rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between two RGB colors. Range: 1.0–21.0."""
    l1 = relative_luminance(rgb1)
    l2 = relative_luminance(rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def wcag_level(ratio: float) -> str:
    """Bucket a contrast ratio into a WCAG label."""
    if ratio >= 7.0:
        return "AAA"
    if ratio >= 4.5:
        return "AA"
    if ratio >= 3.0:
        return "AA-large"
    return "fail"


def passes_wcag_aa(ratio: float, is_large_text: bool = False) -> bool:
    """WCAG AA pass threshold. 4.5 for normal text, 3.0 for large text."""
    return ratio >= (3.0 if is_large_text else 4.5)


def estimate_background_color(
    image: Image.Image,
    box: tuple[int, int, int, int],
    text_color: tuple[int, int, int],
    text_color_threshold: int = 60,
) -> tuple[int, int, int]:
    """Estimate the dominant background color inside ``box`` by averaging the
    pixels that are NOT close to the rendered text color.

    Heuristic: the rendered text occupies a minority of pixels in its bounding
    box; the rest is the background (gradient + photo). Filter out pixels
    within a Euclidean distance threshold of the known text color, then
    average what remains. Falls back to the median of all pixels when the
    filter leaves nothing (e.g. extremely dense text).
    """
    crop = image.crop(box).convert("RGB")
    pixels = list(crop.getdata())
    if not pixels:
        return (0, 0, 0)

    tx, ty, tz = text_color
    threshold_sq = text_color_threshold * text_color_threshold
    background = [
        (r, g, b) for (r, g, b) in pixels
        if (r - tx) ** 2 + (g - ty) ** 2 + (b - tz) ** 2 > threshold_sq
    ]
    sample = background or pixels  # if everything looks like text, fall back

    n = len(sample)
    avg_r = sum(p[0] for p in sample) // n
    avg_g = sum(p[1] for p in sample) // n
    avg_b = sum(p[2] for p in sample) // n
    return (avg_r, avg_g, avg_b)


def evaluate_contrast(
    image_path: str,
    headline_box: tuple[int, int, int, int],
    text_color_hex: str,
    min_ratio: float = 4.5,
    is_large_text: bool = False,
) -> dict:
    """End-to-end: open the rendered PNG, sample the background under the
    headline box, compute the WCAG contrast ratio, and return a result dict
    with everything the QC report needs."""
    img = Image.open(image_path)
    text_rgb = hex_to_rgb(text_color_hex)
    bg_rgb = estimate_background_color(img, tuple(headline_box), text_rgb)
    ratio = contrast_ratio(text_rgb, bg_rgb)
    threshold = 3.0 if is_large_text else min_ratio
    passed = ratio >= threshold
    return {
        "headline_box": list(headline_box),
        "text_color": text_color_hex,
        "background_color": rgb_to_hex(bg_rgb),
        "contrast_ratio": round(ratio, 2),
        "threshold": threshold,
        "wcag_level": wcag_level(ratio),
        "passed": passed,
        "is_large_text": is_large_text,
    }
