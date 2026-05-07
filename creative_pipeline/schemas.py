"""Pydantic v2 schemas for brand guidelines and campaign briefs.

These are the **only** place where input contracts are enforced. Hard-coded
fallbacks in tooling exist solely as safety nets when YAML is silent — every
substantive value (typography sizing, layout knobs, aspect ratios, locale
fallback chains, prompt direction) is read from these models.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class LogoPlacement(str, Enum):
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"


class HeadlineCase(str, Enum):
    SENTENCE = "sentence"
    UPPER = "upper"
    TITLE = "title"
    AS_IS = "as_is"


class CreativeQuality(str, Enum):
    ROUGH_DRAFT = "rough_draft"
    DEMO_POLISHED = "demo_polished"
    PRODUCTION = "production"


class OverlayStyle(str, Enum):
    SCRIM = "scrim"
    VERTICAL_GRADIENT = "vertical_gradient"
    DIAGONAL_GRADIENT = "diagonal_gradient"
    NONE = "none"


class AccentStyle(str, Enum):
    NONE = "none"
    SIDE_RAIL = "side_rail"
    UNDERLINE = "underline"
    COLOR_BLOCK = "color_block"
    SOFT_GLOW = "soft_glow"


class AccentColorRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"
    ACCENT = "accent"


class DisclaimerPlacement(str, Enum):
    UNDER_HEADLINE = "under_headline"
    BOTTOM_CORNER = "bottom_corner"
    BOTTOM_CENTER = "bottom_center"


class LogoTreatment(str, Enum):
    PLAIN = "plain"
    BADGE = "badge"


class PaletteInfluence(str, Enum):
    OFF = "off"
    LIGHT = "light"
    MEDIUM = "medium"
    STRONG = "strong"


class TextAlign(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class HeadlineBackgroundTreatment(str, Enum):
    NONE = "none"
    SOFT_PANEL = "soft_panel"


class DisclaimerBackgroundTreatment(str, Enum):
    NONE = "none"
    SOFT_BADGE = "soft_badge"


class ReadabilityFallback(str, Enum):
    """Ordered escalation steps the composer applies when its contrast
    estimate falls below the brand's threshold. Composer iterates until one
    step pushes the estimate over the bar (or the chain is exhausted)."""
    CHOOSE_BEST_BRAND_TEXT_COLOR = "choose_best_brand_text_color"
    SUBTLE_TEXT_SHADOW = "subtle_text_shadow"
    REPOSITION_WITHIN_TEXT_SAFE_AREA = "reposition_within_text_safe_area"
    SUBTLE_LOCAL_GRADIENT = "subtle_local_gradient"
    SOFT_PANEL_LAST_RESORT = "soft_panel_last_resort"


def _validate_hex(value: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(f"Invalid hex color {value!r}; expected '#RRGGBB'")
    return value.upper()


# -------- Brand --------

class VoiceAndTone(BaseModel):
    personality: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class VisualIdentity(BaseModel):
    primary_color: str
    secondary_color: str
    accent_color: str
    # Backwards-compat lists (used by BrandCheckerAgent's palette match).
    primary_colors: list[str] = Field(default_factory=list)
    accent_colors: list[str] = Field(default_factory=list)
    logo_path: str
    logo_placement: LogoPlacement
    # Composer iterates this list (in order) when the configured
    # ``logo_placement`` collides with the focal/product safe zone.
    # Empty list = legacy fixed-placement behavior.
    logo_allowed_positions: list[LogoPlacement] = Field(default_factory=list)
    # Logo must keep this much breathing room from the focal/product safe
    # zone. ``logo_product_clearance_pct`` is a fraction of min(W, H);
    # ``min_logo_product_gap_px`` per aspect is the absolute floor.
    logo_product_clearance_pct: float = Field(default=0.035, ge=0.0, le=0.30)
    min_logo_product_gap_px: dict[str, int] = Field(default_factory=dict)
    # When True, a logo bbox that overlaps or near-misses the focal safe
    # zone makes the configured placement non-viable; composer falls back
    # to the next entry in ``logo_allowed_positions``.
    hard_fail_logo_product_collision: bool = True
    safe_zone_pct: float = Field(ge=0.0, le=0.5)
    logo_height_pct: float = Field(default=0.10, gt=0.0, le=0.5)
    # Logo treatment: plain paste vs. soft brand-color badge behind the mark.
    logo_treatment: LogoTreatment = LogoTreatment.PLAIN
    logo_badge_opacity: float = Field(default=0.72, ge=0.0, le=1.0)
    logo_badge_color: str = "#FFFFFF"

    @field_validator("logo_badge_color")
    @classmethod
    def _hex_badge(cls, v: str) -> str:
        return _validate_hex(v)

    @field_validator("primary_color", "secondary_color", "accent_color")
    @classmethod
    def _hex_single(cls, v: str) -> str:
        return _validate_hex(v)

    @field_validator("primary_colors", "accent_colors")
    @classmethod
    def _hex_list(cls, v: list[str]) -> list[str]:
        return [_validate_hex(c) for c in v]

    @model_validator(mode="after")
    def _backfill_lists(self) -> "VisualIdentity":
        # If the legacy *_colors lists are empty, derive them from the singletons
        # so existing tooling (BrandChecker palette match) still gets a list.
        if not self.primary_colors:
            self.primary_colors = [self.primary_color, self.secondary_color]
        if not self.accent_colors:
            self.accent_colors = [self.accent_color]
        return self


class PerAspectTypography(BaseModel):
    """Per-aspect-ratio typography targets.

    Composer honors these as hard pixel floors/ceilings + a target vertical
    fill ratio inside the headline box. ``preferred_line_count`` is a soft
    bias: the composer picks the largest size in [min, max] that fits the
    target height; if multiple sizes fit, it prefers ones that wrap to the
    requested line count.
    """
    model_config = ConfigDict(extra="ignore")

    headline_min_font_size_px: int = Field(default=20, gt=0)
    headline_max_font_size_px: int = Field(default=200, gt=0)
    headline_target_zone_fill_pct: float = Field(default=0.50, gt=0.0, le=1.0)
    preferred_line_count: int = Field(default=2, ge=1, le=10)
    disclaimer_min_font_size_px: int = Field(default=12, gt=0)
    disclaimer_max_font_size_px: int = Field(default=80, gt=0)
    # Optional per-aspect override of typography.body_size_ratio (used as
    # initial target for the disclaimer; clamped by min/max).
    disclaimer_font_size_pct_of_height: Optional[float] = Field(default=None, gt=0.0, le=0.2)

    @model_validator(mode="after")
    def _ranges_consistent(self) -> "PerAspectTypography":
        if self.headline_min_font_size_px > self.headline_max_font_size_px:
            raise ValueError(
                f"headline_min_font_size_px ({self.headline_min_font_size_px}) "
                f"must be ≤ headline_max_font_size_px ({self.headline_max_font_size_px})"
            )
        if self.disclaimer_min_font_size_px > self.disclaimer_max_font_size_px:
            raise ValueError(
                f"disclaimer_min_font_size_px ({self.disclaimer_min_font_size_px}) "
                f"must be ≤ disclaimer_max_font_size_px ({self.disclaimer_max_font_size_px})"
            )
        return self


class Typography(BaseModel):
    headline_font: str
    body_font: str
    fonts_dir: str = "fonts"
    text_color_on_dark: str
    text_color_on_light: str
    headline_size_ratio: float = Field(default=0.07, gt=0.0, le=0.5)
    body_size_ratio: float = Field(default=0.022, gt=0.0, le=0.2)
    headline_case: HeadlineCase = HeadlineCase.SENTENCE

    # Aspect-ratio-keyed typography overrides. Any aspect ratio absent from
    # this map falls back to the global headline_size_ratio / body_size_ratio
    # plus the composer's safe pixel-size fallbacks.
    per_aspect: dict[str, PerAspectTypography] = Field(default_factory=dict)

    @field_validator("text_color_on_dark", "text_color_on_light")
    @classmethod
    def _hex(cls, v: str) -> str:
        return _validate_hex(v)


class PerAspectLayout(BaseModel):
    """Per-aspect-ratio overrides for the layout template.

    ``headline_box`` is (x0, y0, x1, y1) as fractions of (W, H) and is the
    fallback / single-zone choice. ``headline_candidate_boxes`` lets the
    composer score several candidate zones and pick the cleanest one
    (highest contrast, lowest texture/edge density). When ``avoid_busy_regions``
    is True, candidate boxes are scored and ranked; otherwise the first
    candidate (or ``headline_box``) is used directly.
    """
    model_config = ConfigDict(extra="ignore")

    headline_box: tuple[float, float, float, float] = (0.05, 0.65, 0.95, 0.95)
    text_align: TextAlign = TextAlign.CENTER

    # Candidate boxes the composer can score (see avoid_busy_regions).
    headline_candidate_boxes: list[tuple[float, float, float, float]] = Field(default_factory=list)
    avoid_busy_regions: bool = False
    # Optional per-aspect overrides for the scoring weights / threshold.
    # When None, sensible defaults apply: busy=0.35, edges=0.45, precheck=3.0.
    busy_region_penalty: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    object_overlap_penalty: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    min_contrast_precheck: Optional[float] = Field(default=None, gt=1.0, le=21.0)
    # Per-aspect line-count constraints applied during headline fitting.
    # The fitter prefers ``preferred_line_count`` (from typography per_aspect)
    # but only accepts wraps with line counts in [min_line_count,
    # max_line_count]. None = no constraint on that side.
    min_line_count: Optional[int] = Field(default=None, ge=1, le=10)
    max_line_count: Optional[int] = Field(default=None, ge=1, le=10)

    @field_validator("headline_box")
    @classmethod
    def _box_in_unit_square(cls, v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = v
        for c in v:
            if not (0.0 <= c <= 1.0):
                raise ValueError(f"headline_box coordinate {c} must be in [0, 1]")
        if x0 >= x1 or y0 >= y1:
            raise ValueError(f"headline_box must satisfy x0<x1 and y0<y1 (got {v})")
        return v

    @field_validator("headline_candidate_boxes")
    @classmethod
    def _candidate_boxes_valid(
        cls, v: list[tuple[float, float, float, float]],
    ) -> list[tuple[float, float, float, float]]:
        for box in v:
            x0, y0, x1, y1 = box
            for c in box:
                if not (0.0 <= c <= 1.0):
                    raise ValueError(f"headline_candidate_boxes coord {c} must be in [0, 1]")
            if x0 >= x1 or y0 >= y1:
                raise ValueError(f"headline_candidate_box must satisfy x0<x1, y0<y1 (got {box})")
        return v


class LayoutTemplate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Legacy "scrim" fields — kept for backwards compatibility. New layouts use
    # overlay_style + overlay_opacity_pct + overlay_extent_pct instead.
    text_region_pct: float = Field(default=0.30, gt=0.0, le=1.0)
    scrim_opacity_pct: float = Field(default=0.40, ge=0.0, le=1.0)
    scrim_padding_pct: float = Field(default=0.04, ge=0.0, le=0.5)
    headline_size_ratio: Optional[float] = Field(default=None, gt=0.0, le=0.5)
    body_size_ratio: Optional[float] = Field(default=None, gt=0.0, le=0.2)
    luminance_threshold: int = Field(default=140, ge=0, le=255)

    # New, polished-layout knobs (all optional with safe defaults).
    overlay_style: OverlayStyle = OverlayStyle.SCRIM
    overlay_opacity_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    overlay_extent_pct: Optional[float] = Field(default=None, gt=0.0, le=1.0)

    accent_style: AccentStyle = AccentStyle.NONE
    accent_color_role: AccentColorRole = AccentColorRole.PRIMARY
    accent_thickness_pct: float = Field(default=0.006, gt=0.0, le=0.05)

    disclaimer_placement: DisclaimerPlacement = DisclaimerPlacement.UNDER_HEADLINE
    disclaimer_padding_pct: float = Field(default=0.025, ge=0.0, le=0.20)

    # Local readability treatments — applied directly behind the rendered text
    # (not as a global scrim). Composer paints a translucent rounded rectangle
    # in the opposite-of-text-color so headlines/disclaimers stay readable
    # over busy photography (striped towels, foliage, etc.) without resorting
    # to a hard dark footer.
    headline_background_treatment: HeadlineBackgroundTreatment = HeadlineBackgroundTreatment.NONE
    headline_panel_opacity_pct: float = Field(default=0.55, ge=0.0, le=1.0)
    headline_panel_padding_pct: float = Field(default=0.025, ge=0.0, le=0.20)
    headline_panel_corner_radius_pct: float = Field(default=0.015, ge=0.0, le=0.50)
    headline_text_shadow: bool = False
    avoid_textured_regions: bool = False

    disclaimer_background_treatment: DisclaimerBackgroundTreatment = DisclaimerBackgroundTreatment.NONE
    disclaimer_badge_opacity_pct: float = Field(default=0.55, ge=0.0, le=1.0)
    disclaimer_badge_padding_pct: float = Field(default=0.020, ge=0.0, le=0.20)
    disclaimer_badge_corner_radius_pct: float = Field(default=0.015, ge=0.0, le=0.50)

    # Ordered fallback chain when the composer's contrast estimate falls below
    # brand.qc threshold. Empty list = no escalation (rely on what
    # _choose_text_treatment already does + whatever explicit treatment is set).
    readability_fallback_order: list[ReadabilityFallback] = Field(default_factory=list)

    # Layout-level toggles for the candidate-headline scorer. When all three
    # are off, the scorer treats every candidate purely on contrast/texture/
    # edge density (existing behavior). When ``avoid_focal_overlap`` is on,
    # the composer estimates a focal-area bbox and penalizes candidate boxes
    # that overlap it by more than focal_area_padding_pct.
    enable_candidate_headline_scoring: bool = True
    avoid_busy_text_regions: bool = True
    avoid_focal_overlap: bool = True
    focal_area_padding_pct: float = Field(default=0.04, ge=0.0, le=0.50)
    # Hard rejection caps applied as normalized fractions [0, 1]. A candidate
    # whose normalized texture or edge score exceeds the cap is "non-viable"
    # and is only chosen when no viable alternative exists.
    text_region_max_edge_density: float = Field(default=0.18, ge=0.0, le=1.0)
    text_region_max_texture_score: float = Field(default=0.55, ge=0.0, le=1.0)
    # Penalty weight for candidate-vs-focal-area intersection (fraction of
    # candidate area inside the focal safe zone).
    focal_overlap_penalty_weight: float = Field(default=0.6, ge=0.0, le=2.0)

    # Object-clearance / breathing-room around focal/product safe zone.
    # ``object_text_clearance_pct`` is a fraction of min(W, H); the composer
    # expands the focal safe zone by this much when scoring candidate boxes.
    # ``min_text_object_gap_px`` is a per-aspect-ratio override in pixels
    # (used by the candidate-shift logic). When a candidate falls inside
    # the expanded zone, ``object_clearance_penalty`` is applied. When it
    # falls inside the *unexpanded* focal safe zone AND
    # ``hard_fail_text_object_collision`` is True, the candidate is marked
    # non-viable (only chosen when no alternative exists).
    object_text_clearance_pct: float = Field(default=0.035, ge=0.0, le=0.30)
    min_text_object_gap_px: dict[str, int] = Field(default_factory=dict)
    object_clearance_penalty: float = Field(default=0.65, ge=0.0, le=2.0)
    hard_fail_text_object_collision: bool = True
    # Near-miss = rendered text bbox sits within ``min_text_object_gap_px``
    # of the unexpanded focal safe zone (no overlap, but visually too close).
    # When true, the composer treats near-miss the same as collision: the
    # candidate is non-viable when alternatives exist, and the post-render
    # shift cascade tries to widen the gap before settling.
    hard_fail_text_object_near_miss: bool = True

    # Crop safety / anti-clipping. The composer scores several crop offsets
    # (constrained by source aspect / target aspect mismatch) and rejects
    # ones whose focal/product cluster lands within
    # ``focal_edge_clearance_pct * min(W, H)`` or ``min_focal_edge_gap_px``
    # of any canvas edge.
    focal_edge_clearance_pct: float = Field(default=0.045, ge=0.0, le=0.30)
    min_focal_edge_gap_px: dict[str, int] = Field(default_factory=dict)
    hard_fail_focal_edge_clip: bool = True
    crop_edge_clip_penalty: float = Field(default=0.90, ge=0.0, le=2.0)

    # Accent rail / underline left-right safe margin. The accent shape
    # cannot cross within ``min_accent_edge_gap_px`` (or
    # ``accent_safe_zone_pct * min(W, H)``) of the canvas edge.
    accent_safe_zone_pct: float = Field(default=0.06, ge=0.0, le=0.20)
    min_accent_edge_gap_px: dict[str, int] = Field(default_factory=dict)

    # Disclaimer candidate boxes (per aspect) — composer scores these for
    # contrast and focal-clearance and picks the best. First box is the
    # configured preference; subsequent ones are alternates.
    disclaimer_candidate_boxes: dict[str, list[tuple[float, float, float, float]]] = (
        Field(default_factory=dict)
    )
    min_disclaimer_object_gap_px: dict[str, int] = Field(default_factory=dict)

    # Adaptive headline-box widening: after the candidate scorer picks
    # a winner, the composer can extend it toward the focal safe zone
    # (up to ``min_text_object_gap_px`` of clearance). The total widen
    # is capped at ``headline_widen_max_pct * canvas_width`` so a
    # pathologically far-right focal cluster doesn't make the headline
    # zone span the entire frame. Set to 0 to disable widening.
    headline_widen_max_pct: float = Field(default=0.10, ge=0.0, le=0.30)

    # Hero-scale threshold for the fitter. When greedy wrap produces
    # single-word lines on a multi-word headline (e.g. narrow box +
    # long words), the fitter normally prefers a smaller font that
    # produces multi-word lines — but if NO size in [min, max] avoids
    # single-word lines, sizes ≥ this threshold are treated as
    # intentional poster-typography and ranked above smaller "tidy"
    # alternatives. Set to a very high number (>= ``headline_max``) to
    # disable the threshold and always prefer multi-word lines when
    # achievable; set to 0 to always prefer the largest font.
    headline_hero_scale_threshold_px: int = Field(default=80, ge=0)

    # Letterbox fallback for unavoidable focal-edge clipping.
    # When every crop offset still clips the focal cluster against a
    # canvas edge, the composer can fit the entire source onto a
    # neutral-color canvas instead of cropping. Disabled by default
    # for backwards compatibility with the legacy crop-only flow.
    enable_focal_letterbox_when_clip_unavoidable: bool = False
    # Color role for the letterbox fill:
    #   "auto"          — sample median edge color, snap to brand if close
    #   "brand_primary" / "brand_secondary" / "brand_accent" — literal brand color
    #   "sampled_edge"  — sample only, no brand snap
    letterbox_color_role: str = Field(default="auto")
    # Maximum letterbox pad as a fraction of min(W, H). When the pad
    # required to clear focal exceeds this cap, the composer skips
    # letterboxing and keeps the legacy clipping behavior.
    letterbox_max_pad_pct: float = Field(default=0.08, ge=0.0, le=0.30)

    text_align_default: TextAlign = TextAlign.CENTER
    per_aspect: dict[str, PerAspectLayout] = Field(default_factory=dict)

    def overlay_opacity(self) -> float:
        """Resolved opacity for whichever overlay style is in use."""
        if self.overlay_opacity_pct is not None:
            return self.overlay_opacity_pct
        return self.scrim_opacity_pct

    def overlay_extent(self) -> float:
        if self.overlay_extent_pct is not None:
            return self.overlay_extent_pct
        return self.text_region_pct


class ImageryStyle(BaseModel):
    mood: str
    avoid: list[str] = Field(default_factory=list)
    style_prompt_suffix: str


class ImageCompositionGuidance(BaseModel):
    """Brand-side photographic composition rules — fed to the image-gen prompt
    so source photography ships with natural negative space for headline copy
    instead of relying on visible composer panels."""
    model_config = ConfigDict(extra="ignore")

    negative_space_required: bool = False
    # Per-aspect-ratio negative-space location, e.g. {"16x9": "left third"}.
    negative_space_location_by_aspect: dict[str, str] = Field(default_factory=dict)
    text_safe_area_prompt: str = ""
    avoid_behind_text: list[str] = Field(default_factory=list)
    preferred_behind_text: list[str] = Field(default_factory=list)
    product_position_by_aspect: dict[str, str] = Field(default_factory=dict)
    depth_of_field: str = ""
    composition_style: str = ""


class LegalRules(BaseModel):
    # English-language disclaimer rendered when localized_legal_copy=False on
    # the brief, regardless of market.
    default_disclaimer: str = "Terms and conditions apply."
    prohibited_words: list[str] = Field(default_factory=list)
    # Per-market localized disclaimer text. Only rendered when the brief has
    # localized_legal_copy=True.
    required_disclaimers: dict[str, str] = Field(default_factory=dict)


class RequiredBrandChecks(BaseModel):
    palette_match: bool = True
    logo_presence: bool = True
    contrast_ratio: bool = True          # WCAG headline-vs-background contrast QC
    disclaimer_contrast: bool = False    # WCAG disclaimer-vs-background contrast QC


class QCRules(BaseModel):
    """Brand-level thresholds for the modular QC rule system."""
    model_config = ConfigDict(extra="ignore")

    min_contrast_ratio: float = Field(default=4.5, gt=1.0, le=21.0)
    large_text_min_ratio: float = Field(default=3.0, gt=1.0, le=21.0)
    large_text_size_threshold_px: int = Field(default=24, gt=0)
    # Disclaimer contrast threshold (always treated as normal-text — small copy
    # should not get the large-text relaxation, regardless of pixel size).
    disclaimer_min_contrast_ratio: float = Field(default=4.5, gt=1.0, le=21.0)


class BrandGuidelines(BaseModel):
    model_config = ConfigDict(extra="ignore")

    brand_id: str = Field(min_length=1)
    brand_name: str = Field(min_length=1)
    voice_and_tone: VoiceAndTone
    visual_identity: VisualIdentity
    typography: Typography
    layout_templates: dict[str, LayoutTemplate] = Field(default_factory=dict)
    aspect_ratios: dict[str, tuple[int, int]] = Field(default_factory=dict)
    imagery_style: ImageryStyle
    creative_quality_presets: dict[CreativeQuality, str] = Field(default_factory=dict)
    legal: LegalRules
    required_brand_checks: RequiredBrandChecks = Field(default_factory=RequiredBrandChecks)
    qc: QCRules = Field(default_factory=QCRules)
    market_locales: dict[str, list[str]] = Field(default_factory=dict)
    image_composition_guidance: Optional[ImageCompositionGuidance] = None

    # Palette guidance — applied during image generation. Brief can override.
    generation_palette_hint: Optional[str] = None
    brand_palette_influence: PaletteInfluence = PaletteInfluence.MEDIUM

    @model_validator(mode="after")
    def _has_at_least_one_layout(self) -> "BrandGuidelines":
        if not self.layout_templates:
            raise ValueError("brand.layout_templates must define at least one template")
        if not self.aspect_ratios:
            raise ValueError("brand.aspect_ratios must define at least one ratio")
        return self


# -------- Campaign brief --------

class Product(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str
    category: str
    description: str
    prompt_keywords: list[str] = Field(default_factory=list)
    # Per-product negative prompt direction (joined with brand.imagery_style.avoid)
    prompt_avoid: list[str] = Field(default_factory=list)
    campaign_message: Optional[str] = None  # optional per-product override


class CampaignBrief(BaseModel):
    model_config = ConfigDict(extra="ignore")

    campaign_id: str = Field(min_length=1)
    campaign_name: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)

    # Localization contract — explicit, not inferred.
    language: str = Field(default="en", min_length=2, max_length=5)
    localized_copy: bool = False
    # When False (default), every market renders the brand's default English
    # disclaimer regardless of brand.legal.required_disclaimers.
    # When True, the per-market localized disclaimer is rendered.
    localized_legal_copy: bool = False

    # Image-source contract — explicit. When True, the asset manager skips its
    # cache lookups so the configured IMAGE_PROVIDER actually fires.
    force_generate_hero: bool = False        # ignore inputs/assets/{pid}/hero.*
    regenerate_cached_assets: bool = False   # ignore outputs/{pid}/source/*

    # QC policy. When True, the QC checker raises after running so the
    # pipeline halts on any failed creative; when False, failures are
    # recorded in the report but the run completes.
    halt_on_qc_failure: bool = False

    target_region: str
    markets: list[str] = Field(min_length=1)
    target_audience: str

    creative_quality: CreativeQuality = CreativeQuality.DEMO_POLISHED
    layout_template: str = Field(min_length=1)

    # Per-campaign overrides for palette guidance. None → fall back to the
    # brand defaults. Brief takes precedence over brand when set.
    brand_palette_influence: Optional[PaletteInfluence] = None
    generation_palette_hint: Optional[str] = None

    # Primary single-language message (rendered when localized_copy=False).
    campaign_message: str = Field(min_length=1)
    # Optional locale-keyed override map (rendered when localized_copy=True).
    campaign_message_localized: dict[str, str] = Field(default_factory=dict)

    # Campaign-specific disclaimer override. When set, this wins over
    # ``brand.legal.default_disclaimer`` / ``brand.legal.required_disclaimers``.
    # Brand legal text remains the fallback when neither field is provided
    # (compliance boilerplate).
    #   - ``disclaimer_text`` (single-language) is rendered when
    #     ``localized_legal_copy=False`` (the default).
    #   - ``disclaimer_text_localized`` is a per-market override map and is
    #     consulted (per market, then per language) when
    #     ``localized_legal_copy=True``.
    # Both are optional; when both are absent the composer falls back to
    # the brand-level legal copy as before.
    disclaimer_text: Optional[str] = None
    disclaimer_text_localized: dict[str, str] = Field(default_factory=dict)

    products: list[Product] = Field(min_length=1)

    @model_validator(mode="after")
    def _localized_consistency(self) -> "CampaignBrief":
        if self.localized_copy:
            if self.language not in self.campaign_message_localized:
                # When localized_copy is on, the primary language must have a localized entry
                # so it can be used as a baseline / fallback. Auto-populate from campaign_message.
                self.campaign_message_localized[self.language] = self.campaign_message
        return self
