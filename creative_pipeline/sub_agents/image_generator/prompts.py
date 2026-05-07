"""Hero-image prompt construction.

Anti-leak rules (gpt-image-1 will bake any literal text into the image):
  - Do NOT pass the headline string into the prompt
  - Do NOT pass the brand name verbatim
  - Do NOT pass the product name verbatim
We describe the product *category* and *visual elements* instead, and add
several explicit "no text" guards.

Composition guidance (brand-side):
  When ``brand.image_composition_guidance`` is set AND the brief uses a layout
  that benefits from natural negative space (currently ``premium_product_hero``),
  the prompt embeds:
    - per-aspect product positioning
    - per-aspect negative-space location
    - text-safe-area description
    - avoid_behind_text and preferred_behind_text lists
    - depth-of-field + composition style hints
  This pushes readability upstream into the photography itself instead of
  letting the composer paint visible panels.

Brand-palette guidance (brief overrides brand defaults):
  - generation_palette_hint
  - brand_palette_influence (off | light | medium | strong)
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
    PaletteInfluence.LIGHT: "Color direction (subtle): introduce a faint hint of ",
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

# Layout templates that should receive the natural-negative-space composition
# guidance (i.e. those whose composer treatments are configured to "none" by
# default). Adding a new such layout = add its name here.
_LAYOUTS_WANTING_COMPOSITION_GUIDANCE = {"premium_product_hero"}


def _join(parts: list[str]) -> str:
    return ". ".join(p.strip(". ") for p in parts if p) + "."


def _resolve_palette(brief: CampaignBrief, brand: BrandGuidelines) -> tuple[PaletteInfluence, str]:
    influence = brief.brand_palette_influence or brand.brand_palette_influence
    hint = brief.generation_palette_hint or brand.generation_palette_hint or ""
    return influence, hint


def _composition_guidance_block(
    brand: BrandGuidelines,
    brief: CampaignBrief,
    hero_aspect_ratio_label: str,
) -> list[str]:
    """Return prompt clauses that describe the desired natural composition,
    or an empty list when the active layout doesn't opt in.

    ``hero_aspect_ratio_label`` is the brand aspect_ratios key the hero is
    rendered at (e.g. '1x1', '16x9'). It selects the per-aspect entries.
    """
    if brief.layout_template not in _LAYOUTS_WANTING_COMPOSITION_GUIDANCE:
        return []

    g = brand.image_composition_guidance
    if g is None or not g.negative_space_required:
        return []

    parts: list[str] = []

    product_pos = g.product_position_by_aspect.get(hero_aspect_ratio_label, "")
    neg_space = g.negative_space_location_by_aspect.get(hero_aspect_ratio_label, "")
    if product_pos:
        parts.append(f"Product position: {product_pos}")
    if neg_space:
        parts.append(f"Negative space for headline copy: {neg_space}")

    if g.text_safe_area_prompt:
        parts.append(g.text_safe_area_prompt.strip())

    if g.preferred_behind_text:
        parts.append(
            "Behind the copy area, prefer: " + "; ".join(g.preferred_behind_text)
        )
    if g.avoid_behind_text:
        parts.append(
            "Behind the copy area, avoid: " + "; ".join(g.avoid_behind_text)
        )

    if g.depth_of_field:
        parts.append(g.depth_of_field)
    if g.composition_style:
        parts.append(g.composition_style)

    # Hard text-leak guards — kept here because the composition block is the
    # most likely place gpt-image-1 will try to "label" the negative space
    # with a fake headline.
    parts.append(
        "DO NOT render any text, words, letters, labels, logos, captions, or "
        "watermarks anywhere in the image — the headline will be added by a "
        "separate compositing step. Leave the copy zone fully clean."
    )

    return parts


def build_prompt(
    product: Product,
    brief: CampaignBrief,
    brand: BrandGuidelines,
    market: Optional[str] = None,
    hero_aspect_ratio_label: str = "1x1",
) -> str:
    """Construct an image-gen prompt that produces a clean, text-free hero
    photo with optional brand-palette guidance and composition negative-space
    direction.

    Args:
        hero_aspect_ratio_label: which brand.aspect_ratios key the hero is
            being generated at (drives per-aspect composition guidance).
    """
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

    composition_block = _composition_guidance_block(brand, brief, hero_aspect_ratio_label)

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
        # Composition guidance block — when the active layout opts in, this
        # block carries the per-aspect product position + negative-space rules.
        *composition_block,
        avoid_clause,
        # Hard guards — repeated even if composition block already included
        # one, because gpt-image-1 ignores single negatives.
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
