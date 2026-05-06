"""Pydantic v2 schemas for brand guidelines and campaign briefs."""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, field_validator, model_validator

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class LogoPlacement(str, Enum):
    TOP_LEFT = "top-left"
    TOP_RIGHT = "top-right"
    BOTTOM_LEFT = "bottom-left"
    BOTTOM_RIGHT = "bottom-right"


def _validate_hex(value: str) -> str:
    if not _HEX_RE.match(value):
        raise ValueError(f"Invalid hex color {value!r}; expected '#RRGGBB'")
    return value.upper()


class VoiceAndTone(BaseModel):
    personality: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)


class VisualIdentity(BaseModel):
    primary_colors: list[str]
    accent_colors: list[str] = Field(default_factory=list)
    logo_path: str
    logo_placement: LogoPlacement
    safe_zone_pct: float = Field(ge=0.0, le=0.5)

    @field_validator("primary_colors", "accent_colors")
    @classmethod
    def _hex_colors(cls, v: list[str]) -> list[str]:
        return [_validate_hex(c) for c in v]


class Typography(BaseModel):
    headline_font: str
    body_font: str
    text_color_on_dark: str
    text_color_on_light: str

    @field_validator("text_color_on_dark", "text_color_on_light")
    @classmethod
    def _hex(cls, v: str) -> str:
        return _validate_hex(v)


class ImageryStyle(BaseModel):
    mood: str
    avoid: list[str] = Field(default_factory=list)
    style_prompt_suffix: str


class LegalRules(BaseModel):
    prohibited_words: list[str] = Field(default_factory=list)
    required_disclaimers: dict[str, str] = Field(default_factory=dict)


class BrandGuidelines(BaseModel):
    brand_id: str = Field(min_length=1)
    voice_and_tone: VoiceAndTone
    visual_identity: VisualIdentity
    typography: Typography
    imagery_style: ImageryStyle
    legal: LegalRules


class Product(BaseModel):
    id: str = Field(min_length=1)
    name: str
    category: str
    description: str


class CampaignBrief(BaseModel):
    campaign_id: str = Field(min_length=1)
    brand_id: str = Field(min_length=1)
    region: str
    markets: list[str] = Field(min_length=1)
    target_audience: str
    campaign_message: dict[str, str]
    products: list[Product] = Field(min_length=1)

    @model_validator(mode="after")
    def _en_required(self) -> CampaignBrief:
        if "en" not in self.campaign_message or not self.campaign_message["en"].strip():
            raise ValueError("campaign_message must include a non-empty 'en' entry")
        return self
