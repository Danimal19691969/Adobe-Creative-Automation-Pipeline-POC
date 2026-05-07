"""Regression tests for brief-flag fidelity: YAML → schema → state → report.

Why this exists: a previous workflow toggled
``inputs/campaign_briefs/summer_refresh_2025.yaml::regenerate_cached_assets``
to false for cache-hit reruns and back to true. The pipeline correctly
recorded the value loaded *at run time*, but a later grep of the YAML
showed the post-restore state, producing the appearance of a YAML/report
mismatch. The loader was correct then and is correct now — these tests
make sure it stays that way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import CampaignBrief
from creative_pipeline.sub_agents.brief_parser.agent import BriefParserAgent
from creative_pipeline.sub_agents.reporter.agent import (
    ReportingAgent,
    _derive_source_origin,
)


def _write_brief(tmp_path: Path, regenerate: bool, force: bool = True) -> Path:
    p = tmp_path / "brief.yaml"
    p.write_text(f"""campaign_id: cf_test
campaign_name: Cache-Flag Test
brand_id: aquacorp_global
language: en
localized_copy: false
localized_legal_copy: false
force_generate_hero: {str(force).lower()}
regenerate_cached_assets: {str(regenerate).lower()}
halt_on_qc_failure: false
target_region: LATAM
markets: ["MX"]
target_audience: x
creative_quality: demo_polished
layout_template: premium_product_hero
campaign_message: Hi
products:
  - id: p1
    name: P
    category: x
    description: y
""")
    return p


# -------- schema-level fidelity --------

@pytest.mark.parametrize("flag_value", [True, False])
def test_schema_loads_regenerate_cached_assets_verbatim(tmp_path, flag_value):
    brief_path = _write_brief(tmp_path, regenerate=flag_value)
    raw = yaml.safe_load(brief_path.read_text())
    brief = CampaignBrief.model_validate(raw)
    assert brief.regenerate_cached_assets is flag_value
    # round-trip through model_dump (this is what brief_parser writes to state)
    dumped = brief.model_dump(mode="json")
    assert dumped["regenerate_cached_assets"] is flag_value


def test_real_yaml_currently_loads_true():
    """Sanity check on the real campaign brief — fails loudly if anyone ever
    flips the flag in the demo YAML by mistake."""
    real_brief = CampaignBrief.model_validate(
        yaml.safe_load(open("inputs/campaign_briefs/summer_refresh_2025.yaml"))
    )
    assert real_brief.regenerate_cached_assets is True
    assert real_brief.force_generate_hero is True


# -------- brief_parser preserves the value end-to-end into session state --------

class _FakeCtx:
    def __init__(self, state: dict):
        self.session = SimpleNamespace(state=state)


def test_brief_parser_preserves_regenerate_cached_assets(tmp_path, monkeypatch):
    brief_path = _write_brief(tmp_path, regenerate=True)
    monkeypatch.setenv("CAMPAIGN_BRIEF_PATH", str(brief_path))

    state = {
        "brand": yaml.safe_load(open("inputs/brand/guidelines.yaml")),
    }

    async def go():
        merged: dict = {}
        async for event in BriefParserAgent(name="bp")._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                merged.update(event.actions.state_delta)
        return merged

    delta = asyncio.run(go())
    assert delta["brief"]["regenerate_cached_assets"] is True
    assert delta["brief"]["force_generate_hero"] is True


# -------- reporter mirrors the loaded brief value verbatim --------

def test_reporter_mirrors_brief_flags(tmp_path, monkeypatch):
    """Reporter reads brief from state and surfaces it in the report.
    True in → True out; no normalization, no stale defaults."""
    monkeypatch.chdir(tmp_path)
    pid = "p1"
    state = {
        "brief": {
            "campaign_id": "x", "campaign_name": "X", "language": "en",
            "force_generate_hero": True,
            "regenerate_cached_assets": True,   # the contested value
            "localized_copy": False, "localized_legal_copy": False,
            "creative_quality": "demo_polished",
            "layout_template": "premium_product_hero",
        },
        "product_ids": [pid],
        f"product:{pid}": {
            "asset_source": "openai_generated",
            "image_provider": "openai", "image_model": "gpt-image-1",
            "image_gen_latency_ms": 30000,
            "used_cache": False,
            "hero_path": "outputs/p1/source/global_x.png",
            "outputs": [],
            "brand_check": {"summary": "pass", "per_output": []},
            "legal_check": {"summary": "pass", "failures": []},
            "qc_check": {"summary": "pass", "failures": [], "rules_run": []},
        },
    }

    async def run_reporter():
        async for _ in ReportingAgent(name="r")._run_async_impl(_FakeCtx(state)):
            pass

    asyncio.run(run_reporter())
    reports = sorted(Path("outputs").glob("report_*.json"))
    report = json.loads(reports[-1].read_text())

    # Run-level mirror
    assert report["regenerate_cached_assets"] is True
    assert report["force_generate_hero"] is True

    # Per-product source_origin = generated_this_run when used_cache=False
    # AND asset_source is a *_generated value.
    assert report["products"][0]["source_origin"] == "generated_this_run"


# -------- source_origin derivation --------

def test_source_origin_generated_this_run():
    assert _derive_source_origin({
        "asset_source": "openai_generated", "used_cache": False,
    }) == "generated_this_run"


def test_source_origin_reused_generated_previous_run():
    assert _derive_source_origin({
        "asset_source": "openai_generated", "used_cache": True,
    }) == "reused_generated_previous_run"
    assert _derive_source_origin({
        "asset_source": "imagen_generated", "used_cache": True,
    }) == "reused_generated_previous_run"
    # Sidecar missing → asset_source falls back to "generated_cached".
    assert _derive_source_origin({
        "asset_source": "generated_cached", "used_cache": True,
    }) == "reused_generated_previous_run"


def test_source_origin_reused_local_placeholder():
    assert _derive_source_origin({
        "asset_source": "user_supplied", "used_cache": True,
    }) == "reused_local_placeholder"


def test_source_origin_no_source():
    assert _derive_source_origin({
        "asset_source": None, "used_cache": False, "hero_path": None,
    }) == "no_source"
