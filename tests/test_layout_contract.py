"""Tests for the premium_product_hero layout contract.

Covers:
  - Brand YAML defines premium_product_hero with the expected fields.
  - Brief YAML references premium_product_hero by name and the brief_parser
    cross-validates that the named template exists.
  - Per-aspect headline boxes are honored (output reports them in pixel coords).
  - Vertical-gradient overlay style flows through to the report.
  - Logo badge treatment is recorded.
  - Brand checker emits brand_palette_score, brand_element_score, and
    brand_check_reason for every output.
  - Palette hint reaches the image-gen prompt when brand_palette_influence
    is at least "light"; not when "off".
  - Composer keeps the disclaimer English when localized_legal_copy=False
    even with the new layout.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import (
    AccentStyle,
    BrandGuidelines,
    CampaignBrief,
    DisclaimerPlacement,
    LogoTreatment,
    OverlayStyle,
    PaletteInfluence,
)
from creative_pipeline.sub_agents.brand_checker.agent import BrandCheckerAgent
from creative_pipeline.sub_agents.creative_composer.agent import CreativeComposerAgent
from creative_pipeline.sub_agents.image_generator.prompts import build_prompt


@pytest.fixture
def brand_yaml() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


@pytest.fixture
def brief_yaml() -> CampaignBrief:
    with open("inputs/campaign_briefs/summer_refresh_2025.yaml") as f:
        return CampaignBrief.model_validate(yaml.safe_load(f))


# -------- Brand YAML defines the new layout --------

def test_brand_defines_premium_product_hero(brand_yaml):
    layout = brand_yaml.layout_templates.get("premium_product_hero")
    assert layout is not None, "brand YAML must define premium_product_hero"
    assert layout.overlay_style == OverlayStyle.VERTICAL_GRADIENT
    assert layout.accent_style != AccentStyle.NONE
    assert layout.disclaimer_placement == DisclaimerPlacement.BOTTOM_CORNER
    # Per-aspect boxes for all three production aspect ratios.
    for ratio in ("1x1", "9x16", "16x9"):
        assert ratio in layout.per_aspect, f"missing per-aspect entry for {ratio}"


def test_brief_uses_premium_product_hero(brief_yaml, brand_yaml):
    assert brief_yaml.layout_template == "premium_product_hero"
    assert brief_yaml.layout_template in brand_yaml.layout_templates


def test_logo_treatment_badge_loaded(brand_yaml):
    assert brand_yaml.visual_identity.logo_treatment == LogoTreatment.BADGE
    assert 0.0 <= brand_yaml.visual_identity.logo_badge_opacity <= 1.0


# -------- Palette hint flows into the prompt --------

def test_palette_hint_in_prompt_when_influence_medium(brand_yaml, brief_yaml):
    p = build_prompt(brief_yaml.products[0], brief_yaml, brand_yaml)
    # The hint phrase from the brief should appear (or its substantive words).
    hint = brief_yaml.generation_palette_hint or brand_yaml.generation_palette_hint
    assert hint is not None
    # Several words from the hint must show up in the prompt verbatim.
    for needle in ("cyan", "navy", "white"):
        assert needle in p.lower(), f"palette hint word {needle!r} not in prompt"


def test_palette_hint_absent_when_influence_off(brand_yaml, brief_yaml):
    # Mutate a copy of the brief to force influence=off.
    custom = brief_yaml.model_copy(update={"brand_palette_influence": PaletteInfluence.OFF})
    p = build_prompt(custom.products[0], custom, brand_yaml)
    # The intensity-prefixed phrase shouldn't appear.
    assert "Color direction" not in p


# -------- Composer respects per-aspect boxes + records layout metadata --------

class _FakeCtx:
    def __init__(self, state: dict):
        self.session = SimpleNamespace(state=state)


def _run_composer(agent: CreativeComposerAgent, state: dict) -> dict:
    async def go():
        last_delta: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                last_delta.update(event.actions.state_delta)
        return last_delta
    return asyncio.run(go())


def _make_state(brand: BrandGuidelines, brief: CampaignBrief, hero_path: str, pid: str) -> dict:
    return {
        "brand": brand.model_dump(mode="json"),
        "brief": brief.model_dump(mode="json"),
        f"product:{pid}": {
            "hero_path": hero_path, "asset_source": "user_supplied", "used_cache": True,
        },
    }


def _stage_brand_assets(tmp_path):
    """Copy the real brand logo into tmp_path so logo-stamp paths in the brand
    YAML resolve after the test chdir's into tmp_path."""
    import shutil
    src_logo = Path(__file__).parent.parent / "inputs" / "assets" / "global" / "logo.png"
    dst_logo = tmp_path / "inputs" / "assets" / "global" / "logo.png"
    dst_logo.parent.mkdir(parents=True, exist_ok=True)
    if src_logo.exists():
        shutil.copy(src_logo, dst_logo)
    src_fonts = Path(__file__).parent.parent / "fonts"
    dst_fonts = tmp_path / "fonts"
    if src_fonts.exists():
        shutil.copytree(src_fonts, dst_fonts, dirs_exist_ok=True)


def test_composer_records_layout_boxes_and_overlay_metadata(brand_yaml, brief_yaml, tmp_path, monkeypatch):
    _stage_brand_assets(tmp_path)
    monkeypatch.chdir(tmp_path)
    pid = brief_yaml.products[0].id
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (1024, 1024), (180, 220, 240)).save(hero)

    # The composer reads brand.layout_templates by name; our brief points at
    # premium_product_hero, but we need the brand fixture to also be findable
    # via the agent's runtime path. tmp_path mode chdir's away — bring the
    # brand into state directly.
    state = _make_state(brand_yaml, brief_yaml, str(hero), pid)
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    delta = _run_composer(agent, state)

    outputs = delta[f"product:{pid}"]["outputs"]
    assert outputs, "composer produced no outputs"

    layout = brand_yaml.layout_templates["premium_product_hero"]
    by_ratio = {o["ratio"]: o for o in outputs}

    # Every ratio must be present and carry the new layout metadata.
    for ratio, dims in brand_yaml.aspect_ratios.items():
        o = by_ratio[ratio]
        W, H = dims
        # headline_box pixel coords match the per-aspect fractional box.
        per = layout.per_aspect[ratio]
        bx0, by0, bx1, by1 = per.headline_box
        # The composer reports headline_box as [x0, y0_top_of_text, x1, y1_bottom_of_text]
        # where y0 == box top (pixels) and y1 is inferred from rendered text height.
        hb = o["headline_box"]
        assert hb[0] == int(bx0 * W)
        assert hb[1] == int(by0 * H)
        assert hb[2] == int(bx1 * W)
        # bottom is inferred but must lie within the box vertical extent.
        assert int(by0 * H) <= hb[3] <= int(by1 * H) + 4

        # Overlay + accent + disclaimer placement metadata recorded.
        assert o["overlay_style"] == "vertical_gradient"
        assert 0.0 <= o["overlay_opacity"] <= 1.0
        assert o["accent_style"] == "side_rail"
        assert o["disclaimer_box"] is not None
        assert o["logo_position"] is not None
        assert o["logo_size_px"] is not None
        assert o["logo_box"] is not None
        # logo_box bounds are positive and within canvas.
        x0, y0, x1, y1 = o["logo_box"]
        assert 0 <= x0 < x1 <= W
        assert 0 <= y0 < y1 <= H


# -------- localized_legal_copy=false stays English under the new layout --------

def test_disclaimer_english_under_premium_layout(brand_yaml, brief_yaml, tmp_path, monkeypatch):
    """Regression: with the new layout, localized_legal_copy=false must still
    produce the English default_disclaimer in every market."""
    _stage_brand_assets(tmp_path)
    monkeypatch.chdir(tmp_path)
    pid = brief_yaml.products[0].id
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (1024, 1024), (180, 220, 240)).save(hero)

    assert brief_yaml.localized_legal_copy is False
    state = _make_state(brand_yaml, brief_yaml, str(hero), pid)
    agent = CreativeComposerAgent(name="CC", product_id=pid)
    delta = _run_composer(agent, state)

    outputs = delta[f"product:{pid}"]["outputs"]
    for o in outputs:
        assert o["disclaimer_text"] == brand_yaml.legal.default_disclaimer
        assert "Aplican" not in o["disclaimer_text"]
        assert "Consulte" not in o["disclaimer_text"]


# -------- Brand checker emits scores + reason --------

def test_brand_checker_emits_scores_and_reason(brand_yaml, brief_yaml, tmp_path, monkeypatch):
    _stage_brand_assets(tmp_path)
    monkeypatch.chdir(tmp_path)
    pid = brief_yaml.products[0].id
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (1024, 1024), (180, 220, 240)).save(hero)

    # Composer first to produce real PNG outputs the brand checker reads.
    state = _make_state(brand_yaml, brief_yaml, str(hero), pid)
    composer = CreativeComposerAgent(name="CC", product_id=pid)
    delta = _run_composer(composer, state)
    state.update(delta)

    checker = BrandCheckerAgent(name="BC", product_id=pid)

    async def go():
        last_delta: dict = {}
        async for event in checker._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                last_delta.update(event.actions.state_delta)
        return last_delta

    bc_delta = asyncio.run(go())
    bc = bc_delta[f"product:{pid}"]["brand_check"]
    assert "summary" in bc
    assert "reason" in bc
    assert bc["per_output"], "brand checker produced no per-output rows"
    for row in bc["per_output"]:
        assert "brand_palette_score" in row
        assert "brand_element_score" in row
        assert "brand_check_reason" in row
        assert 0 <= row["brand_palette_score"] <= 100
        assert 0 <= row["brand_element_score"] <= 100


# -------- Reporter surfaces every layout/score field --------

def test_reporter_surfaces_layout_and_scores(brand_yaml, brief_yaml, tmp_path, monkeypatch):
    """End-to-end: composer + brand checker + reporter — confirm the report's
    output rows include headline_box, disclaimer_box, logo_box, logo_position,
    logo_size_px, overlay_style, overlay_opacity, brand_palette_score,
    brand_element_score, brand_check_reason."""
    import json
    from creative_pipeline.sub_agents.reporter.agent import ReportingAgent

    _stage_brand_assets(tmp_path)
    monkeypatch.chdir(tmp_path)
    pid = brief_yaml.products[0].id
    asset_dir = tmp_path / "inputs" / "assets" / pid
    asset_dir.mkdir(parents=True)
    hero = asset_dir / "hero.png"
    Image.new("RGB", (1024, 1024), (180, 220, 240)).save(hero)

    state = _make_state(brand_yaml, brief_yaml, str(hero), pid)
    state["product_ids"] = [pid]
    state["product:" + pid]["asset_source"] = "openai_generated"
    state["product:" + pid]["image_provider"] = "openai"
    state["product:" + pid]["image_model"] = "gpt-image-1"

    # composer
    composer = CreativeComposerAgent(name="CC", product_id=pid)
    state.update(_run_composer(composer, state))
    # brand checker
    checker = BrandCheckerAgent(name="BC", product_id=pid)

    async def go_check():
        d: dict = {}
        async for event in checker._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                d.update(event.actions.state_delta)
        return d

    state.update(asyncio.run(go_check()))

    # legal stub so reporter has it
    state["product:" + pid]["legal_check"] = {"summary": "pass", "failures": []}

    reporter = ReportingAgent(name="Rep")

    async def go_report():
        async for _ in reporter._run_async_impl(_FakeCtx(state)):
            pass

    asyncio.run(go_report())
    reports = sorted(Path("outputs").glob("report_*.json"))
    assert reports
    report = json.loads(reports[-1].read_text())

    out0 = report["products"][0]["outputs"][0]
    for field in (
        "headline_box", "disclaimer_box", "logo_box",
        "logo_position", "logo_size_px",
        "overlay_style", "overlay_opacity",
        "accent_style", "accent_color",
        "brand_palette_score", "brand_element_score", "brand_check_reason",
    ):
        assert field in out0, f"report row missing field {field!r}"
    assert isinstance(out0["headline_box"], list) and len(out0["headline_box"]) == 4
    assert isinstance(out0["logo_box"], list) and len(out0["logo_box"]) == 4
