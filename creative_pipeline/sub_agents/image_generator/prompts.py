"""Hero-image prompt construction. Pure-string concatenation, fully input-driven.

Anti-leak rules (gpt-image-1 will bake any literal text into the image):
  - Do NOT pass the headline string into the prompt
  - Do NOT pass the brand name verbatim
  - Do NOT pass the product name verbatim
We describe the product *category* and *visual elements* instead, and add
several explicit "no text" guards.

Brand-palette guidance (brief overrides brand defaults):
  - generation_palette_hint: free-form description of preferred colorways
  - brand_palette_influence: off | light | medium | strong → wording intensity
"""

from __future__ import annotations

from typing import Optional

from creative_pipeline.schemas import (
    BrandGuidelines,
    CampaignBrief,
    PaletteInfluence,
    Product,
)


_PALETTE_INFLUENCE_PHRASING: dict[PaletteInfluence, str] = {
    PaletteInfluence.OFF: "",
    PaletteInfluence.LIGHT: (
        "Color direction (subtle): introduce a faint hint of "
    ),
    PaletteInfluence.MEDIUM: (
        "Color direction: weave the following palette into the natural lighting, "
        "reflections, sky, water, and product highlights so the photo feels "
        "compatible with the brand without becoming a flat graphic — "
    ),
    PaletteInfluence.STRONG: (
        "Color direction (strong): make the dominant palette of the photograph "
        "lean clearly toward — "
    ),
}


def _join(parts: list[str]) -> str:
    return ". ".join(p.strip(". ") for p in parts if p) + "."


def _resolve_palette(brief: CampaignBrief, brand: BrandGuidelines) -> tuple[PaletteInfluence, str]:
    """Brief overrides brand defaults for palette guidance."""
    influence = brief.brand_palette_influence or brand.brand_palette_influence
    hint = brief.generation_palette_hint or brand.generation_palette_hint or ""
    return influence, hint


def build_prompt(
    product: Product,
    brief: CampaignBrief,
    brand: BrandGuidelines,
    market: Optional[str] = None,
) -> str:
    """Construct an image-gen prompt that produces a clean, text-free hero photo
    with optional brand-palette guidance."""
    quality_preset = brand.creative_quality_presets.get(brief.creative_quality, "")

    keyword_clause = ""
    if product.prompt_keywords:
        keyword_clause = "Visual elements: " + "; ".join(product.prompt_keywords)

    avoid_terms = list(product.prompt_avoid) + list(brand.imagery_style.avoid)
    avoid_clause = (
        "Negative direction (do NOT include): " + "; ".join(avoid_terms)
        if avoid_terms else ""
    )

    influence, palette_hint = _resolve_palette(brief, brand)
    palette_clause = ""
    if influence != PaletteInfluence.OFF and palette_hint:
        palette_clause = _PALETTE_INFLUENCE_PHRASING[influence] + palette_hint
        if influence in (PaletteInfluence.MEDIUM, PaletteInfluence.STRONG):
            palette_clause += (
                ". Keep it photorealistic — natural materials, real product, "
                "real lighting; do not flatten into a graphic illustration"
            )

    parts = [
        "Premium social media campaign hero photograph",
        f"Subject: a {product.category} product, depicted via the visual elements below",
        keyword_clause,
        f"Audience cue: {brief.target_audience}, region {brief.target_region}",
        f"Mood: {brand.imagery_style.mood}",
        f"Personality: {', '.join(brand.voice_and_tone.personality)}",
        f"Style: {brand.imagery_style.style_prompt_suffix}",
        quality_preset,
        palette_clause,
        # Composition keeps room for the headline that will be overlaid in a
        # separate Pillow pass.
        "Composition: hero subject centered or upper third; leave the bottom 35% "
        "of the frame visually clean and uncluttered (no objects, no surfaces "
        "with high-contrast detail) so a headline can overlay it cleanly",
        avoid_clause,
        # Hard guards — repeated because gpt-image-1 ignores single negatives.
        "ABSOLUTELY NO TEXT in the image: no words, no letters, no numbers, "
        "no labels on the product, no brand wordmarks, no captions, no "
        "watermarks, no chalkboard signs, no menu boards, no books with "
        "visible text, no posters, no packaging copy",
        "The product packaging must be blank — no printed text or numbers of "
        "any kind on the bottle, can, tube, or label",
        "If you would normally add a label, leave it BLANK or render it as a "
        "plain colored sticker with no text or symbols",
    ]
    return _join(parts)
