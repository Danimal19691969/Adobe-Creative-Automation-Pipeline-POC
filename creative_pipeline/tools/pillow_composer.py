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

from creative_pipeline.schemas import (
    AccentColorRole,
    AccentStyle,
    BrandGuidelines,
    DisclaimerPlacement,
    HeadlineCase,
    LayoutTemplate,
    LogoPlacement,
    LogoTreatment,
    OverlayStyle,
    PerAspectLayout,
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
    logo = Image.open(vi.logo_path).convert("RGBA")

    target_h = int(min(W, H) * (vi.logo_height_pct or _FALLBACKS["logo_height_pct"]))
    aspect = logo.size[0] / logo.size[1]
    target_w = max(1, int(target_h * aspect))
    logo = logo.resize((target_w, target_h), Image.LANCZOS)

    safe_x = int(W * vi.safe_zone_pct)
    safe_y = int(H * vi.safe_zone_pct)
    logo_w, logo_h = target_w, target_h

    if vi.logo_treatment == LogoTreatment.BADGE:
        # Build a soft circular/rounded badge slightly larger than the logo.
        pad = int(target_h * 0.18)
        badge_w = target_w + pad * 2
        badge_h = target_h + pad * 2
        badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
        bd = ImageDraw.Draw(badge)
        rgba = _hex_to_rgb(vi.logo_badge_color) + (int(vi.logo_badge_opacity * 255),)
        # Rounded square (visually a "chip" — works for both square and tall logos).
        bd.rounded_rectangle((0, 0, badge_w, badge_h), radius=int(min(badge_w, badge_h) * 0.5), fill=rgba)
        # Place logo inside the badge.
        badge.alpha_composite(logo, dest=(pad, pad))
        pos = _logo_position(W, H, badge_w, badge_h, vi.logo_placement, safe_x, safe_y)
        canvas.alpha_composite(badge, dest=pos)
        logo_w, logo_h = badge_w, badge_h
    else:
        pos = _logo_position(W, H, logo_w, logo_h, vi.logo_placement, safe_x, safe_y)
        canvas.alpha_composite(logo, dest=pos)

    return canvas, {
        "position": list(pos),
        "size_px": [logo_w, logo_h],
        "treatment": vi.logo_treatment.value,
    }


# ----- composition pipeline -------------------------------------------------


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
    canvas = smart_crop(hero, target_w, target_h)

    # 1) overlay
    canvas = _render_overlay(canvas, layout)

    # 2) headline + disclaimer in the per-aspect headline box
    per_aspect = _resolve_per_aspect(layout, ratio)
    bx0, by0, bx1, by1 = per_aspect.headline_box
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
    initial_headline_size = max(20, int(target_h * headline_ratio))

    # Reserve disclaimer height inside the headline box only when placement is
    # under_headline so we don't over-shrink the headline.
    reserve_for_disclaimer = (
        disclaimer is not None and layout.disclaimer_placement == DisclaimerPlacement.UNDER_HEADLINE
    )
    headline_max_h = box_h
    if reserve_for_disclaimer:
        headline_max_h = int(box_h * 0.78)

    draw = ImageDraw.Draw(canvas)
    headline_font, headline_lines, headline_total_h = _fit_text_to_box(
        draw, cased_headline,
        guidelines.typography.headline_font, fonts_dir,
        initial_headline_size, box_w, headline_max_h,
        line_spacing=1.16,
    )

    # Vertical anchor: top of headline = box top (headline grows downward).
    headline_top = hb[1]
    headline_bottom = headline_top + headline_total_h

    # Pick text color from luminance under the headline strip *after* overlay.
    luma_threshold = layout.luminance_threshold or _FALLBACKS["luminance_threshold"]
    sample_region = (hb[0], headline_top, hb[2], min(headline_bottom, target_h))
    luma_img = canvas.crop(sample_region).convert("L").resize((1, 1), Image.BOX)
    luma = float(luma_img.getpixel((0, 0)))
    text_hex = (
        guidelines.typography.text_color_on_dark
        if luma < luma_threshold
        else guidelines.typography.text_color_on_light
    )
    text_rgb = _hex_to_rgb(text_hex) + (255,)

    # Render headline lines.
    lh = _line_height(draw, headline_font)
    line_step = int(lh * 1.16)
    y = headline_top
    for line in headline_lines:
        bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = bbox[2] - bbox[0]
        x = _align_x(hb[0], hb[2], line_w, per_aspect.text_align)
        draw.text((x, y), line, font=headline_font, fill=text_rgb)
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
    if disclaimer:
        db = _disclaimer_box(
            target_w, target_h, hb, headline_bottom,
            layout.disclaimer_placement, layout.disclaimer_padding_pct,
        )
        d_w = max(1, db[2] - db[0])
        d_h = max(1, db[3] - db[1])
        d_initial_size = max(_FALLBACKS["min_font_size"], int(target_h * body_ratio))
        draw_d = ImageDraw.Draw(canvas)
        d_font, d_lines, d_total_h = _fit_text_to_box(
            draw_d, disclaimer,
            guidelines.typography.body_font, fonts_dir,
            d_initial_size, d_w, d_h,
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
        for line in d_lines:
            bbox = draw_d.textbbox((0, 0), line, font=d_font)
            lw = bbox[2] - bbox[0]
            x = _align_x(db[0], db[2], lw, d_align)
            draw_d.text((x, d_y_start), line, font=d_font, fill=text_rgb)
            d_y_start += d_step
        disclaimer_box_meta = [db[0], db[1], db[2], db[3]]

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
        "overlay_opacity": layout.overlay_opacity(),
        "accent_style": layout.accent_style.value,
        "accent_color": accent_color_hex if layout.accent_style != AccentStyle.NONE else None,
    }


# Keep the legacy entry points exported so older imports keep working.
def add_text_overlay(*args, **kwargs):  # pragma: no cover — kept for back-compat
    raise RuntimeError(
        "add_text_overlay() is deprecated; use compose_creative() which orchestrates "
        "the full overlay/headline/accent/logo pipeline."
    )
