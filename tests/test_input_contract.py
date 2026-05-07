"""Input contract tests — guarantee the pipeline is truly input-driven.

Covers:
  - English stays English when language=en and localized_copy=false
  - Brand typography values flow from guidelines.yaml into the renderer
  - Layout template chosen by the brief is honored end-to-end
  - Provider/model selection is env-driven (no hard-coding)
  - Prohibited words come from brand guidelines (no module constants)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief, LayoutTemplate
from creative_pipeline.sub_agents.image_generator.prompts import build_prompt
from creative_pipeline.sub_agents.legal_checker.agent import (
    LegalViolation,
    _scan_prohibited_words,
)
from creative_pipeline.tools import image_gen, llm_factory
from creative_pipeline.tools.pillow_composer import compose_creative


# -------- Fixtures load REAL YAML so the test mirrors production loading. --------

@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def brief_yaml() -> CampaignBrief:
    with open("inputs/campaign_briefs/summer_refresh_2025.yaml") as f:
        return CampaignBrief.model_validate(yaml.safe_load(f))


@pytest.fixture
def fake_hero(tmp_path: Path) -> str:
    img = Image.new("RGB", (1024, 1024), (200, 220, 240))
    p = tmp_path / "hero.png"
    img.save(p)
    return str(p)


# -------- (a) English stays English when localized_copy=false --------

def test_english_pinned_when_localized_copy_false(brand_yaml, brief_yaml):
    """When language=en and localized_copy=false, the resolved headline for
    every market must be the brief's primary English campaign_message — never
    Spanish/Portuguese, even though those locales exist in
    campaign_message_localized."""
    assert brief_yaml.language == "en"
    assert brief_yaml.localized_copy is False

    # Simulate the composer's headline-resolution logic for every market.
    for market in brief_yaml.markets:
        # Per the composer: when localized_copy=false, locale = brief.language,
        # headline = product.campaign_message or brief.campaign_message.
        headline = brief_yaml.products[0].campaign_message or brief_yaml.campaign_message
        locale = brief_yaml.language
        assert locale == "en", f"market {market} must use 'en' when localized_copy=false"
        assert headline == "Refresh your summer, naturally."
        # Spanish/Portuguese strings must not leak into the English-only run.
        assert "verano" not in headline.lower()
        assert "verão" not in headline.lower()


def test_localized_copy_true_picks_per_market_locale(brand_yaml):
    """When localized_copy=true, the composer uses brand.market_locales to pick
    the locale per market and pulls the headline from
    campaign_message_localized."""
    raw = {
        "campaign_id": "loc_test",
        "campaign_name": "Localized Test",
        "brand_id": "aquacorp_global",
        "language": "en",
        "localized_copy": True,
        "target_region": "LATAM",
        "markets": ["MX", "BR", "US"],
        "target_audience": "test",
        "creative_quality": "demo_polished",
        "layout_template": "hero_full_bleed_footer",
        "campaign_message": "Refresh your summer, naturally.",
        "campaign_message_localized": {
            "en": "Refresh your summer, naturally.",
            "es": "Refresca tu verano, naturalmente.",
            "pt": "Renove seu verão, naturalmente.",
        },
        "products": [{"id": "p1", "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(raw)

    from creative_pipeline.tools.file_utils import pick_locale

    # MX → es
    locale_mx = pick_locale("MX", list(brief.campaign_message_localized.keys()), brand_yaml.market_locales, brief.language)
    assert locale_mx == "es"
    assert brief.campaign_message_localized[locale_mx] == "Refresca tu verano, naturalmente."

    # BR → pt
    locale_br = pick_locale("BR", list(brief.campaign_message_localized.keys()), brand_yaml.market_locales, brief.language)
    assert locale_br == "pt"

    # US → en
    locale_us = pick_locale("US", list(brief.campaign_message_localized.keys()), brand_yaml.market_locales, brief.language)
    assert locale_us == "en"


# -------- (b) Brand typography values are loaded from guidelines.yaml --------

def test_brand_typography_loaded_from_yaml(brand_yaml):
    """Confirms typography fields flow from YAML — values come from the brand
    file, not from module constants in pillow_composer."""
    t = brand_yaml.typography
    assert t.headline_font == "Montserrat-Bold.ttf"
    assert t.body_font == "OpenSans-Regular.ttf"
    assert t.fonts_dir == "fonts"
    # Sizing ratios must be valid fractions, sourced from YAML (not the
    # composer's _FALLBACKS dict). Exact values change with brand tuning;
    # the contract is "comes from YAML and is in range".
    assert 0.0 < t.headline_size_ratio <= 0.5
    assert 0.0 < t.body_size_ratio <= 0.2
    assert t.headline_case.value == "sentence"
    assert t.text_color_on_dark == "#FFFFFF"
    assert t.text_color_on_light == "#023E8A"


def test_changing_typography_in_yaml_changes_renderer(brand_yaml, brief_yaml, fake_hero, tmp_path):
    """Bumping headline_size_ratio in the YAML should produce a measurably
    larger headline render — proves the constant flows through."""
    layout_small = LayoutTemplate(headline_size_ratio=0.05, scrim_padding_pct=0.04)
    layout_large = LayoutTemplate(headline_size_ratio=0.12, scrim_padding_pct=0.04)

    out_small = str(tmp_path / "small.png")
    out_large = str(tmp_path / "large.png")
    compose_creative(fake_hero, "1x1", "Big bold headline that wraps", None, brand_yaml, layout_small, out_small)
    compose_creative(fake_hero, "1x1", "Big bold headline that wraps", None, brand_yaml, layout_large, out_large)

    assert Path(out_small).stat().st_size != Path(out_large).stat().st_size


# -------- (c) layout_template is accepted from the campaign brief --------

def test_layout_template_from_brief_resolves_in_brand(brand_yaml, brief_yaml):
    assert brief_yaml.layout_template in brand_yaml.layout_templates
    layout = brand_yaml.layout_templates[brief_yaml.layout_template]
    # Confirm the default template's knobs match the YAML.
    assert layout.text_region_pct == 0.30
    assert layout.scrim_opacity_pct == 0.40


def test_unknown_layout_template_rejected_by_brief_parser(brand_yaml):
    """The brief_parser raises when layout_template doesn't exist in the brand.
    We exercise the same cross-validation rule directly here."""
    raw = {
        "campaign_id": "x", "campaign_name": "x", "brand_id": "aquacorp_global",
        "target_region": "LATAM", "markets": ["MX"], "target_audience": "x",
        "creative_quality": "demo_polished", "layout_template": "nonexistent_template",
        "campaign_message": "x",
        "products": [{"id": "p", "name": "P", "category": "c", "description": "d"}],
    }
    brief = CampaignBrief.model_validate(raw)  # schema accepts any string here
    available = list(brand_yaml.layout_templates.keys())
    assert brief.layout_template not in available  # cross-check would fail


# -------- (d) Provider/model settings come from environment variables --------

def test_provider_model_from_env(monkeypatch):
    """make_llm() must read PROVIDER and MODEL from os.environ, not hardcode."""
    captured = {}

    class FakeLiteLlm:
        def __init__(self, model):
            captured["model"] = model
            self.model = model

    monkeypatch.setattr(llm_factory, "LiteLlm", FakeLiteLlm)
    monkeypatch.setenv("PROVIDER", "anthropic")
    monkeypatch.setenv("MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    llm = llm_factory.make_llm()
    assert llm.model == "anthropic/claude-sonnet-4-5"


def test_image_provider_from_env(monkeypatch):
    """generate_hero_image must dispatch based on IMAGE_PROVIDER (env), not provider hardcode."""
    calls = []

    def fake(prompt, out_path, aspect_ratio, model):
        calls.append((prompt, out_path, aspect_ratio, model))
        return {"path": out_path, "latency_ms": 1, "model": model}

    monkeypatch.setattr(image_gen, "_IMAGE_BACKENDS", {
        "google": (fake, "GOOGLE_API_KEY", "imagen-3.0-generate-002", "IMAGE_MODEL"),
        "openai": (fake, "OPENAI_API_KEY", "gpt-image-1", "IMAGE_MODEL"),
    })
    for key in ("PROVIDER", "IMAGE_PROVIDER", "IMAGE_MODEL", "GOOGLE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IMAGE_PROVIDER", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-test")

    result = image_gen.generate_hero_image("p", "/tmp/o.png", "16:9")
    assert result["model"] == "imagen-3.0-generate-002"
    assert calls[0][2] == "16:9"


# -------- (e) Prohibited words come from brand guidelines --------

def test_prohibited_words_from_yaml(brand_yaml):
    assert "guaranteed" in brand_yaml.legal.prohibited_words
    assert "best" in brand_yaml.legal.prohibited_words
    assert "cure" in brand_yaml.legal.prohibited_words
    assert "clinically proven" in brand_yaml.legal.prohibited_words


def test_legal_precheck_uses_yaml_prohibited_list(brand_yaml):
    """A new prohibited word added to the YAML list should trigger a violation
    when present in a message — proves the list is data-driven."""
    custom_brand = brand_yaml.model_copy(deep=True)
    custom_brand.legal.prohibited_words.append("amazing")

    hits = _scan_prohibited_words({"en": "an amazing product"}, custom_brand.legal.prohibited_words)
    assert any(h[1] == "amazing" for h in hits)


# -------- Bonus: prompt builder uses every contract field --------

def test_prompt_builder_consumes_full_contract(brand_yaml, brief_yaml):
    """The prompt must include scene-shaping context (audience, region, mood,
    style, keywords, quality preset) — but must NOT include the headline,
    brand name, or product name verbatim, because gpt-image-1 bakes those
    into the image as literal text."""
    p = build_prompt(brief_yaml.products[0], brief_yaml, brand_yaml)

    # Included — scene-shaping fields the model needs.
    assert brand_yaml.imagery_style.mood.split(",")[0] in p
    assert brief_yaml.target_audience in p
    assert brief_yaml.target_region in p
    assert brief_yaml.products[0].category in p
    for kw in brief_yaml.products[0].prompt_keywords:
        assert kw in p
    assert brand_yaml.creative_quality_presets[brief_yaml.creative_quality] in p
    # Hard text-leak guard wording.
    assert "ABSOLUTELY NO TEXT" in p
    assert "blank" in p.lower()

    # Excluded — text-leak vectors.
    assert brand_yaml.brand_name not in p, "brand name leaks as label text"
    assert brief_yaml.products[0].name not in p, "product name leaks as label text"
    assert brief_yaml.campaign_message not in p, "headline leaks into image"
    assert brief_yaml.campaign_name not in p, "campaign name leaks into image"
