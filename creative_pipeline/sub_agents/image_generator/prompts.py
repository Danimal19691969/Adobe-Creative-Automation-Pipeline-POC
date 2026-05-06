"""Hero-image prompt construction. Pure-string concatenation per spec §7.4."""

from __future__ import annotations

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief, Product


def build_prompt(product: Product, brief: CampaignBrief, brand: BrandGuidelines) -> str:
    """Construct the Imagen / gpt-image-1 prompt for a hero shot of one product."""
    return (
        f"{product.name} ({product.category}). {product.description}. "
        f"Target audience: {brief.target_audience}. "
        f"Region: {brief.region}. "
        f"Style: {brand.imagery_style.style_prompt_suffix}. "
        f"Mood: {brand.imagery_style.mood}. "
        f"Personality: {', '.join(brand.voice_and_tone.personality)}. "
        f"Avoid: {', '.join(brand.imagery_style.avoid)}."
    )
