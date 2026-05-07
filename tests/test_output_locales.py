"""Tests for the explicit ``output_locales`` fan-out model.

Decouples *distribution market* (``brief.markets``) from *rendered language*
(``brief.output_locales``). When set, the composer fans out as
``markets × output_locales`` per product per ratio.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml
from PIL import Image
from pydantic import ValidationError

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.sub_agents.creative_composer.agent import CreativeComposerAgent


# -------- shared helpers --------

class _FakeCtx:
    def __init__(self, state: dict):
        class _Session:
            def __init__(self, s): self.state = s
        self.session = _Session(state)


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


def _make_brief(brand_id: str, layout: str, **overrides) -> dict:
    """Minimal valid brief dict; override fields per test."""
    base = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_id,
        "language": "en", "localized_copy": True, "localized_legal_copy": True,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["US"], "target_audience": "x",
        "creative_quality": "demo_polished", "layout_template": layout,
        "campaign_message": "Refresh your summer, naturally.",
        "campaign_message_localized": {
            "en": "Refresh your summer, naturally.",
            "es": "Refresca tu verano, naturalmente.",
            "pt": "Renove seu verão, naturalmente.",
        },
        "disclaimer_text_localized": {
            "en": "Terms and conditions apply.",
            "es": "Aplican términos y condiciones.",
            "pt": "Consulte os termos e condições.",
        },
        "products": [{"id": "p1", "name": "P", "category": "x", "description": "y"}],
    }
    base.update(overrides)
    return base


def _run_composer(brand: BrandGuidelines, brief: CampaignBrief, hero_path: str, pid: str) -> list[dict]:
    """Drive the composer to completion against a pre-rendered hero, return outputs list."""
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {
            "hero_path": hero_path,
            "asset_source": "user_supplied",
            "used_cache": True,
        },
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    return delta[f"product:{pid}"]["outputs"]


# -------- 1. Schema accepts a valid output_locales list --------

def test_output_locales_schema_accepts_valid_list(brand_yaml):
    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        output_locales=["en", "es", "pt"],
    ))
    assert brief.output_locales == ["en", "es", "pt"]


# -------- 2. Schema rejects locales missing from campaign_message_localized --------

def test_output_locales_schema_rejects_missing_localized_message(brand_yaml):
    raw = _make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        output_locales=["en", "fr"],  # fr not in campaign_message_localized
    )
    with pytest.raises(ValidationError) as exc_info:
        CampaignBrief.model_validate(raw)
    msg = str(exc_info.value)
    assert "fr" in msg
    assert "campaign_message_localized" in msg


def test_output_locales_schema_rejects_empty_list(brand_yaml):
    raw = _make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        output_locales=[],
    )
    with pytest.raises(ValidationError):
        CampaignBrief.model_validate(raw)


def test_output_locales_schema_rejects_duplicates(brand_yaml):
    raw = _make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        output_locales=["en", "es", "en"],
    )
    with pytest.raises(ValidationError):
        CampaignBrief.model_validate(raw)


# -------- 3. Composer produces one file per locale per market per ratio --------

def test_output_locales_generates_one_file_per_locale_per_market(brand_yaml, tmp_path, monkeypatch):
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    hero = tmp_path / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        markets=["US"],
        output_locales=["en", "es", "pt"],
    ))
    outputs = _run_composer(brand_yaml, brief, str(hero), pid)

    n_ratios = len(brand_yaml.aspect_ratios)
    # 1 market × 3 locales × n_ratios.
    assert len(outputs) == 1 * 3 * n_ratios

    # Every (market, locale) tuple must show up at every ratio.
    pairs = {(o["market"], o["locale"]) for o in outputs}
    assert pairs == {("US", "en"), ("US", "es"), ("US", "pt")}

    # Filenames carry market + locale.
    paths = {o["path"] for o in outputs}
    assert any("US_en" in p for p in paths)
    assert any("US_es" in p for p in paths)
    assert any("US_pt" in p for p in paths)


# -------- 4. Headlines + disclaimers come from localized maps per locale --------

def test_output_locales_picks_correct_localized_headline_and_disclaimer(brand_yaml, tmp_path, monkeypatch):
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    hero = tmp_path / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        markets=["US"],
        output_locales=["en", "es", "pt"],
    ))
    outputs = _run_composer(brand_yaml, brief, str(hero), pid)

    for o in outputs:
        if o["locale"] == "en":
            assert o["headline"] == "Refresh your summer, naturally."
            assert o["disclaimer_text"] == "Terms and conditions apply."
        elif o["locale"] == "es":
            assert o["headline"] == "Refresca tu verano, naturalmente."
            assert o["disclaimer_text"] == "Aplican términos y condiciones."
        elif o["locale"] == "pt":
            assert o["headline"] == "Renove seu verão, naturalmente."
            assert o["disclaimer_text"] == "Consulte os termos e condições."


# -------- 5. Backward-compat: absent output_locales preserves existing behavior --------

def test_output_locales_absent_preserves_existing_behavior(brand_yaml, tmp_path, monkeypatch):
    """Without output_locales, localized_copy=true still produces per-market
    locale output (MX→es, BR→pt) via brand.market_locales — i.e. the existing
    test_localized_copy_true_picks_per_market_locale contract."""
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    hero = tmp_path / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        markets=["MX", "BR"],
        # No output_locales.
    ))
    assert brief.output_locales is None
    outputs = _run_composer(brand_yaml, brief, str(hero), pid)

    n_ratios = len(brand_yaml.aspect_ratios)
    # 2 markets × 1 locale per market × n_ratios.
    assert len(outputs) == 2 * n_ratios

    pairs = {(o["market"], o["locale"]) for o in outputs}
    assert ("MX", "es") in pairs
    assert ("BR", "pt") in pairs


# -------- 6. Cardinality: markets × output_locales (no surprise growth) --------

def test_output_locales_cardinality_is_markets_times_locales(brand_yaml, tmp_path, monkeypatch):
    """Brief author controls cardinality via the markets list.
    markets=[US] × output_locales=[en,es,pt] = 3 per ratio (not 9).
    markets=[US, CA] × output_locales=[en,es] = 4 per ratio.
    """
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    hero = tmp_path / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        markets=["US", "CA"],
        output_locales=["en", "es"],
    ))
    outputs = _run_composer(brand_yaml, brief, str(hero), pid)

    n_ratios = len(brand_yaml.aspect_ratios)
    assert len(outputs) == 2 * 2 * n_ratios

    pairs = {(o["market"], o["locale"]) for o in outputs}
    assert pairs == {("US", "en"), ("US", "es"), ("CA", "en"), ("CA", "es")}


# -------- 7. Image-gen prompt cue stays tied to brief.language --------

def test_image_prompt_cue_stays_tied_to_brief_language_under_output_locales(brand_yaml):
    """User decision: one source hero per product, language cue follows
    brief.language regardless of output_locales. Per-locale heroes are out
    of scope (would 3x Imagen API cost)."""
    from creative_pipeline.sub_agents.image_generator.prompts import build_prompt

    brief = CampaignBrief.model_validate(_make_brief(
        brand_id=brand_yaml.brand_id,
        layout=next(iter(brand_yaml.layout_templates.keys())),
        markets=["US"],
        output_locales=["en", "es", "pt"],
        language="en",
    ))
    prompt = build_prompt(brief.products[0], brief, brand_yaml)
    assert "English-speaking" in prompt
    assert "Spanish-speaking" not in prompt
    assert "Portuguese-speaking" not in prompt
