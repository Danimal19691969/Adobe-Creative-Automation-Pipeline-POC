"""Tests for the demo-polished force-generation contract.

Covers:
  - force_generate_hero=True bypasses inputs/assets/{pid}/hero.*
  - regenerate_cached_assets=True bypasses outputs/{pid}/source/* cache
  - When both flags are True, AssetManagerAgent yields hero_path=None so the
    image generator must fire (asset_source is not "user_supplied").
  - localized_legal_copy=False renders the brand's English default_disclaimer
    in every market regardless of brand.legal.required_disclaimers.
  - Image-tool result populates image_model + provider + asset_source.
  - Reporter surfaces source_asset_path + asset_source per output.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief, LegalRules
from creative_pipeline.sub_agents.asset_manager.agent import AssetManagerAgent
from creative_pipeline.sub_agents.creative_composer.agent import CreativeComposerAgent
from creative_pipeline.tools import image_gen


# -------- Asset manager: flag gating --------

class _FakeCtx:
    """Minimal stand-in for InvocationContext used by AssetManagerAgent."""
    def __init__(self, state: dict):
        class _Session:
            def __init__(self, s): self.state = s
        self.session = _Session(state)


def _drain(agent: AssetManagerAgent, state: dict) -> dict:
    """Run the async generator to completion; return the state_delta of the final event."""
    async def go():
        last_delta: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                last_delta.update(event.actions.state_delta)
        return last_delta
    return asyncio.run(go())


def test_force_generate_hero_skips_local_asset(tmp_path, monkeypatch):
    """When force_generate_hero=True, AssetManagerAgent must not return the local
    inputs/assets/{pid}/hero.png even if it exists."""
    monkeypatch.chdir(tmp_path)
    pid = "p1"
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    Image.new("RGB", (16, 16)).save(asset_dir / "hero.png")

    state = {"brief": {"force_generate_hero": True, "regenerate_cached_assets": True}}
    delta = _drain(AssetManagerAgent(name="AM", product_id=pid), state)

    product_state = delta[f"product:{pid}"]
    assert product_state["hero_path"] is None
    assert product_state["asset_source"] != "user_supplied"
    assert product_state["used_cache"] is False


def test_force_generate_off_uses_local_asset(tmp_path, monkeypatch):
    """Sanity baseline: with both flags False, the local asset is reused."""
    monkeypatch.chdir(tmp_path)
    pid = "p1"
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    local_path = asset_dir / "hero.png"
    Image.new("RGB", (16, 16)).save(local_path)

    state = {"brief": {"force_generate_hero": False, "regenerate_cached_assets": False}}
    delta = _drain(AssetManagerAgent(name="AM", product_id=pid), state)

    product_state = delta[f"product:{pid}"]
    # Asset manager uses relative paths (Path("inputs/assets/...")); compare endings.
    assert product_state["hero_path"].endswith(f"inputs/assets/{pid}/hero.png")
    assert product_state["asset_source"] == "user_supplied"
    assert product_state["used_cache"] is True


def test_regenerate_cached_assets_skips_generated_cache(tmp_path, monkeypatch):
    """With force_generate=True (no local asset path) and regenerate_cached_assets=True,
    a previously-generated source file must be skipped."""
    monkeypatch.chdir(tmp_path)
    pid = "p1"
    cache_dir = tmp_path / "outputs" / pid / "source"
    cache_dir.mkdir(parents=True)
    cached = cache_dir / "global_20260101T000000Z.png"
    Image.new("RGB", (16, 16)).save(cached)
    cached.with_suffix(".json").write_text(json.dumps({
        "asset_source": "openai_generated", "image_model": "gpt-image-1",
        "image_provider": "openai", "image_gen_latency_ms": 1234,
    }))

    state = {"brief": {"force_generate_hero": True, "regenerate_cached_assets": True}}
    delta = _drain(AssetManagerAgent(name="AM", product_id=pid), state)
    product_state = delta[f"product:{pid}"]
    assert product_state["hero_path"] is None
    assert product_state["used_cache"] is False


def test_regenerate_cached_assets_off_reuses_generated_cache(tmp_path, monkeypatch):
    """With force_generate=True (skip local) but regenerate_cached_assets=False,
    a previously-generated source file IS reused with full provenance."""
    monkeypatch.chdir(tmp_path)
    pid = "p1"
    cache_dir = tmp_path / "outputs" / pid / "source"
    cache_dir.mkdir(parents=True)
    cached = cache_dir / "global_20260101T000000Z.png"
    Image.new("RGB", (16, 16)).save(cached)
    cached.with_suffix(".json").write_text(json.dumps({
        "asset_source": "openai_generated", "image_model": "gpt-image-1",
        "image_provider": "openai", "image_gen_latency_ms": 1234,
    }))

    state = {"brief": {"force_generate_hero": True, "regenerate_cached_assets": False}}
    delta = _drain(AssetManagerAgent(name="AM", product_id=pid), state)
    product_state = delta[f"product:{pid}"]
    assert product_state["hero_path"].endswith(f"outputs/{pid}/source/global_20260101T000000Z.png")
    assert product_state["asset_source"] == "openai_generated"
    assert product_state["image_model"] == "gpt-image-1"
    assert product_state["image_provider"] == "openai"
    assert product_state["image_gen_latency_ms"] == 1234
    assert product_state["used_cache"] is True


# -------- Image-gen dispatcher: provenance fields --------

def test_dispatcher_returns_provider_and_asset_source(monkeypatch):
    monkeypatch.delenv("PROVIDER", raising=False)
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)
    # IMAGE_MODEL must be cleared too: package init now loads .env with
    # override=True, so any IMAGE_MODEL set in the project's .env
    # (e.g. gemini-2.5-flash-image) leaks into the test process and
    # would override the backend's default model below.
    monkeypatch.delenv("IMAGE_MODEL", raising=False)
    monkeypatch.setenv("IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    def fake_openai(prompt, out_path, aspect_ratio, model):
        return {"path": out_path, "latency_ms": 9, "model": model}

    monkeypatch.setattr(image_gen, "_IMAGE_BACKENDS", {
        "google": (fake_openai, "GOOGLE_API_KEY", "imagen-3.0-generate-002", "IMAGE_MODEL"),
        "openai": (fake_openai, "OPENAI_API_KEY", "gpt-image-1", "IMAGE_MODEL"),
    })

    result = image_gen.generate_hero_image("a prompt", "/tmp/out.png", "1:1")
    assert result["model"] == "gpt-image-1"
    assert result["provider"] == "openai"
    assert result["asset_source"] == "openai_generated"


# -------- Composer: localized_legal_copy off → English disclaimer --------

@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


def test_default_disclaimer_in_english(brand_yaml):
    assert brand_yaml.legal.default_disclaimer == "Terms and conditions apply."


def test_localized_legal_copy_false_uses_default_in_every_market(brand_yaml, tmp_path, monkeypatch):
    """When localized_legal_copy=False, the composer must render the default
    English disclaimer in every market — even MX/BR/CO which have entries in
    brand.legal.required_disclaimers."""
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": False, "localized_legal_copy": False,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["MX", "BR", "CO"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "Refresh your summer, naturally.",
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)

    # Stand up the composer with state pre-seeded to a cache hit on the local hero.
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {"hero_path": str(hero), "asset_source": "user_supplied", "used_cache": True},
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    outputs = delta[f"product:{pid}"]["outputs"]
    # Every output across MX/BR/CO must use the English default disclaimer.
    for o in outputs:
        assert o["disclaimer_text"] == "Terms and conditions apply."
        assert "Aplican" not in o["disclaimer_text"]
        assert "Consulte" not in o["disclaimer_text"]


def test_brief_disclaimer_text_overrides_brand_default(brand_yaml, tmp_path, monkeypatch):
    """Campaign-specific disclaimer set on the brief wins over
    ``brand.legal.default_disclaimer`` when ``localized_legal_copy=False``.
    Brand legal stays as compliance fallback for briefs that don't set it."""
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": False, "localized_legal_copy": False,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["MX", "BR", "CO"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "Refresh your summer, naturally.",
        "disclaimer_text": "Promotion ends August 31, 2025.",
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {"hero_path": str(hero), "asset_source": "user_supplied", "used_cache": True},
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    outputs = delta[f"product:{pid}"]["outputs"]
    # Brief override must win in every market — the brand's default
    # ("Terms and conditions apply.") is the fallback, not the override.
    for o in outputs:
        assert o["disclaimer_text"] == "Promotion ends August 31, 2025."
        assert "Terms and conditions" not in o["disclaimer_text"]


def test_brief_disclaimer_localized_overrides_brand_required(brand_yaml, tmp_path, monkeypatch):
    """When ``localized_legal_copy=True``, brief.disclaimer_text_localized[market]
    wins over brand.legal.required_disclaimers[market]."""
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": True, "localized_legal_copy": True,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["MX", "BR"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "Refresh your summer, naturally.",
        "campaign_message_localized": {"en": "Refresh your summer, naturally."},
        "disclaimer_text_localized": {
            "MX": "Promoción válida hasta el 31 de agosto.",
            "BR": "Promoção válida até 31 de agosto.",
        },
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {"hero_path": str(hero), "asset_source": "user_supplied", "used_cache": True},
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    outputs = delta[f"product:{pid}"]["outputs"]
    by_market = {o["market"]: o["disclaimer_text"] for o in outputs}
    assert "Promoción" in by_market["MX"]
    assert "Promoção" in by_market["BR"]
    # The brand-level required_disclaimers ("Aplican términos…") is the
    # fallback when the brief doesn't override that market — but here
    # both markets are overridden, so brand legal is silent.
    assert "Aplican" not in by_market["MX"]
    assert "Consulte" not in by_market["BR"]


def test_brand_legal_falls_back_when_brief_silent(brand_yaml, tmp_path, monkeypatch):
    """When neither ``brief.disclaimer_text`` nor
    ``brief.disclaimer_text_localized`` is set, the composer falls back
    to brand.legal — the compliance boilerplate stays as the safety net."""
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": False, "localized_legal_copy": False,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["MX"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "Refresh your summer, naturally.",
        # no disclaimer_text, no disclaimer_text_localized
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {"hero_path": str(hero), "asset_source": "user_supplied", "used_cache": True},
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    outputs = delta[f"product:{pid}"]["outputs"]
    assert outputs[0]["disclaimer_text"] == brand_yaml.legal.default_disclaimer


def test_localized_legal_copy_true_uses_per_market_text(brand_yaml, tmp_path, monkeypatch):
    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": True, "localized_legal_copy": True,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "target_region": "LATAM", "markets": ["MX", "BR"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "Refresh your summer, naturally.",
        "campaign_message_localized": {
            "en": "Refresh your summer, naturally.",
            "es": "Refresca tu verano, naturalmente.",
            "pt": "Renove seu verão, naturalmente.",
        },
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)

    agent = CreativeComposerAgent(name="CC", product_id=pid)
    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {"hero_path": str(hero), "asset_source": "user_supplied", "used_cache": True},
    }

    async def go():
        deltas: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    outputs = delta[f"product:{pid}"]["outputs"]
    by_market = {o["market"]: o["disclaimer_text"] for o in outputs}
    assert "Aplican términos y condiciones." in by_market["MX"]
    assert "Consulte os termos e condições." in by_market["BR"]


# -------- Reporter: per-output provenance fields --------

def test_report_includes_provenance_fields_per_output(brand_yaml, tmp_path, monkeypatch):
    """Run the composer + reporter directly and confirm the report rows include
    source_asset_path, asset_source, image_provider, used_cache, force/regen
    flags, language, localized_*, layout_template, creative_quality, disclaimer_text."""
    from creative_pipeline.sub_agents.reporter.agent import ReportingAgent

    pid = "p1"
    monkeypatch.chdir(tmp_path)
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(hero)

    brief_dict = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand_yaml.brand_id,
        "language": "en", "localized_copy": False, "localized_legal_copy": False,
        "force_generate_hero": True, "regenerate_cached_assets": True,
        "target_region": "LATAM", "markets": ["MX"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand_yaml.layout_templates.keys())),
        "campaign_message": "x",
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
    }
    brief = CampaignBrief.model_validate(brief_dict)

    state = {
        "brand": brand_yaml.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        "product_ids": [pid],
        f"product:{pid}": {
            "hero_path": str(tmp_path / "outputs" / pid / "source" / "global_x.png"),
            "asset_source": "openai_generated",
            "image_provider": "openai",
            "image_model": "gpt-image-1",
            "image_gen_latency_ms": 4321,
            "used_cache": False,
            "outputs": [{
                "market": "MX", "locale": "en", "ratio": "1x1",
                "path": str(tmp_path / "outputs" / pid / "1x1" / "MX_en.png"),
                "headline": "x", "disclaimer_text": "Terms and conditions apply.",
                "disclaimer_rendered": True,
            }],
            "brand_check": {"summary": "pass", "per_output": []},
            "legal_check": {"summary": "pass", "failures": []},
        },
    }

    agent = ReportingAgent(name="Reporter")

    async def go():
        async for _ in agent._run_async_impl(_FakeCtx(state)):
            pass

    asyncio.run(go())
    reports = sorted(Path("outputs").glob("report_*.json"))
    assert reports, "reporter must write a JSON file"
    report = json.loads(reports[-1].read_text())

    assert report["force_generate_hero"] is True
    assert report["regenerate_cached_assets"] is True
    assert report["localized_legal_copy"] is False
    assert report["language"] == "en"
    assert report["layout_template"] == brief.layout_template
    assert report["creative_quality"] == "demo_polished"

    out0 = report["products"][0]["outputs"][0]
    for k in ("source_asset_path", "asset_source", "image_provider", "image_model",
              "used_cache", "force_generate_hero", "regenerate_cached_assets",
              "language", "localized_copy", "localized_legal_copy",
              "disclaimer_text", "layout_template", "creative_quality"):
        assert k in out0, f"output row missing field {k!r}"
    assert out0["asset_source"] == "openai_generated"
    assert out0["image_model"] == "gpt-image-1"
    assert out0["disclaimer_text"] == "Terms and conditions apply."
    assert out0["used_cache"] is False
