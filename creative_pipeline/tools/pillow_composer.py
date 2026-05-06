"""Pure-Pillow creative composition: smart crop, text overlay, logo stamp."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from creative_pipeline.schemas import BrandGuidelines, LogoPlacement
from creative_pipeline.tools.file_utils import ASPECT_RATIOS

logger = logging.getLogger(__name__)

_FONTS_DIR = Path("fonts")
_HEADLINE_TEXT_PCT = 0.07   # headline font size as fraction of canvas height
_DISCLAIMER_TEXT_PCT = 0.022
_TEXT_REGION_PCT = 0.30     # bottom 30% of canvas reserved for text
_SCRIM_OPACITY = int(0.40 * 255)  # spec §7.5: 40% black scrim for legibility
_SCRIM_PADDING_PCT = 0.04
_LOGO_HEIGHT_PCT = 0.10     # logo height as fraction of canvas min dimension


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def smart_crop(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Crop + resize to exact target. Bias the vertical crop toward the upper third
    where product hero shots typically live."""
    src_w, src_h = img.size
    target_aspect = target_w / target_h
    src_aspect = src_w / src_h

    if abs(src_aspect - target_aspect) < 0.01:
        return img.resize((target_w, target_h), Image.LANCZOS)

    if src_aspect > target_aspect:
        # Source wider than target — crop horizontally, keep full height
        new_w = int(src_h * target_aspect)
        x0 = (src_w - new_w) // 2
        cropped = img.crop((x0, 0, x0 + new_w, src_h))
    else:
        # Source taller than target — crop vertically, biased upward
        new_h = int(src_w / target_aspect)
        # Bias: place crop at 25% from top instead of 50%
        max_y0 = src_h - new_h
        y0 = min(max_y0, max(0, int(max_y0 * 0.25)))
        cropped = img.crop((0, y0, src_w, y0 + new_h))

    return cropped.resize((target_w, target_h), Image.LANCZOS)


def _region_luminance(img: Image.Image, region: tuple[int, int, int, int]) -> float:
    """Average perceived luminance (0–255) of the given (x0,y0,x1,y1) region."""
    # Pillow's "L" mode applies Rec. 601 luma (0.299R + 0.587G + 0.114B).
    luma = img.crop(region).convert("L")
    bbox = luma.getbbox()
    if not bbox:
        return 0.0
    stat = luma.resize((1, 1), Image.BOX)
    return float(stat.getpixel((0, 0)))


def _load_font(font_filename: str, size: int) -> ImageFont.FreeTypeFont:
    path = _FONTS_DIR / font_filename
    if not path.exists():
        logger.warning("Font %s not found in %s, falling back to default", font_filename, _FONTS_DIR)
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
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


def add_text_overlay(
    img: Image.Image,
    headline: str,
    disclaimer: str | None,
    guidelines: BrandGuidelines,
) -> Image.Image:
    """Render the campaign message + (optional) disclaimer in the bottom text region.
    Choose text color based on luminance under the text region. Add a scrim for legibility."""
    canvas = img.convert("RGBA")
    W, H = canvas.size

    region_h = int(H * _TEXT_REGION_PCT)
    region = (0, H - region_h, W, H)

    # Apply scrim first, then evaluate luminance against what the eye will actually see.
    scrim = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    scrim_draw.rectangle(region, fill=(0, 0, 0, _SCRIM_OPACITY))
    canvas = Image.alpha_composite(canvas, scrim)

    luma = _region_luminance(canvas, region)
    text_color_hex = (
        guidelines.typography.text_color_on_dark
        if luma < 140
        else guidelines.typography.text_color_on_light
    )
    text_rgb = _hex_to_rgb(text_color_hex)

    draw = ImageDraw.Draw(canvas)

    headline_size = max(20, int(H * _HEADLINE_TEXT_PCT))
    headline_font = _load_font(guidelines.typography.headline_font, headline_size)
    pad = int(W * _SCRIM_PADDING_PCT)
    max_text_width = W - 2 * pad

    headline_lines = _wrap_text(draw, headline, headline_font, max_text_width)

    # Layout headline at top of text region with padding.
    y_cursor = H - region_h + pad
    for line in headline_lines:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        x = (W - line_w) // 2
        draw.text((x, y_cursor), line, font=headline_font, fill=text_rgb + (255,))
        y_cursor += int(line_h * 1.2)

    if disclaimer:
        disclaimer_size = max(12, int(H * _DISCLAIMER_TEXT_PCT))
        disclaimer_font = _load_font(guidelines.typography.body_font, disclaimer_size)
        disclaimer_lines = _wrap_text(draw, disclaimer, disclaimer_font, max_text_width)
        # Anchor disclaimer to bottom with padding
        line_heights = []
        for line in disclaimer_lines:
            b = draw.textbbox((0, 0), line, font=disclaimer_font)
            line_heights.append(b[3] - b[1])
        total_disc_h = sum(int(lh * 1.2) for lh in line_heights)
        y_cursor = H - pad - total_disc_h
        for line, lh in zip(disclaimer_lines, line_heights):
            b = draw.textbbox((0, 0), line, font=disclaimer_font)
            line_w = b[2] - b[0]
            x = (W - line_w) // 2
            draw.text((x, y_cursor), line, font=disclaimer_font, fill=text_rgb + (255,))
            y_cursor += int(lh * 1.2)

    return canvas


def stamp_logo(
    img: Image.Image,
    logo_path: str,
    placement: LogoPlacement,
    safe_zone_pct: float,
) -> Image.Image:
    """Paste the logo into the configured corner with the safe-zone margin."""
    if not os.path.exists(logo_path):
        logger.warning("Logo file not found at %s — skipping logo stamp", logo_path)
        return img

    canvas = img.convert("RGBA")
    W, H = canvas.size
    logo = Image.open(logo_path).convert("RGBA")

    # Resize logo to fixed % of min canvas dimension while preserving aspect.
    target_h = int(min(W, H) * _LOGO_HEIGHT_PCT)
    aspect = logo.size[0] / logo.size[1]
    target_w = int(target_h * aspect)
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    margin_x = int(W * safe_zone_pct)
    margin_y = int(H * safe_zone_pct)

    if placement == LogoPlacement.TOP_LEFT:
        pos = (margin_x, margin_y)
    elif placement == LogoPlacement.TOP_RIGHT:
        pos = (W - target_w - margin_x, margin_y)
    elif placement == LogoPlacement.BOTTOM_LEFT:
        pos = (margin_x, H - target_h - margin_y)
    else:  # BOTTOM_RIGHT
        pos = (W - target_w - margin_x, H - target_h - margin_y)

    canvas.paste(logo, pos, logo)
    return canvas


def compose_creative(
    hero_path: str,
    ratio: str,
    headline: str,
    disclaimer: str | None,
    guidelines: BrandGuidelines,
    out_path: str,
) -> dict:
    """Compose one creative for one (ratio, market) and write to out_path.
    Returns metadata dict consumed by CreativeComposerAgent."""
    if ratio not in ASPECT_RATIOS:
        raise ValueError(f"Unknown ratio {ratio!r}; expected one of {list(ASPECT_RATIOS)}")
    target_w, target_h = ASPECT_RATIOS[ratio]

    started_ms = time.monotonic()
    hero = Image.open(hero_path).convert("RGBA")
    cropped = smart_crop(hero, target_w, target_h)
    with_text = add_text_overlay(cropped, headline, disclaimer, guidelines)
    with_logo = stamp_logo(
        with_text,
        guidelines.visual_identity.logo_path,
        guidelines.visual_identity.logo_placement,
        guidelines.visual_identity.safe_zone_pct,
    )

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with_logo.convert("RGB").save(out_path, "PNG")
    duration_ms = int((time.monotonic() - started_ms) * 1000)

    return {
        "path": out_path,
        "ratio": ratio,
        "size": (target_w, target_h),
        "duration_ms": duration_ms,
        "disclaimer_rendered": disclaimer is not None,
    }
