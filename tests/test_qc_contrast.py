"""Tests for the WCAG contrast QC system.

Covers:
  - Pure WCAG math (relative_luminance / contrast_ratio / wcag_level / passes_wcag_aa).
  - estimate_background_color filters out text-color pixels.
  - ContrastRule passes/fails as expected on synthetic high/low-contrast images.
  - QCCheckerAgent end-to-end:
      * passes when contrast is high
      * fails (severity=fail) when contrast is low
      * halt_on_qc_failure=True raises QCFailure
      * halt_on_qc_failure=False records the failure but does not raise
  - Reporter surfaces qc_check + contrast_ratio + wcag_level + qc_rules per output.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.sub_agents.qc_checker.agent import QCCheckerAgent, QCFailure
from creative_pipeline.tools.contrast import (
    contrast_ratio,
    estimate_background_color,
    evaluate_contrast,
    passes_wcag_aa,
    relative_luminance,
    wcag_level,
)
from creative_pipeline.tools.qc_rules import ContrastRule, build_rules


# -------- WCAG math (pure) --------

def test_relative_luminance_white_is_one():
    assert relative_luminance((255, 255, 255)) == pytest.approx(1.0, abs=1e-6)


def test_relative_luminance_black_is_zero():
    assert relative_luminance((0, 0, 0)) == pytest.approx(0.0, abs=1e-6)


def test_contrast_ratio_white_on_black_is_21():
    assert contrast_ratio((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0, abs=0.01)


def test_contrast_ratio_same_color_is_one():
    assert contrast_ratio((128, 128, 128), (128, 128, 128)) == pytest.approx(1.0, abs=1e-6)


def test_contrast_ratio_is_symmetric():
    a = contrast_ratio((255, 255, 255), (50, 50, 50))
    b = contrast_ratio((50, 50, 50), (255, 255, 255))
    assert a == pytest.approx(b)


def test_wcag_level_buckets():
    assert wcag_level(21.0) == "AAA"
    assert wcag_level(7.0) == "AAA"
    assert wcag_level(4.5) == "AA"
    assert wcag_level(3.0) == "AA-large"
    assert wcag_level(2.9) == "fail"


def test_passes_wcag_aa_normal_text_threshold_is_4_5():
    assert passes_wcag_aa(4.5) is True
    assert passes_wcag_aa(4.49) is False


def test_passes_wcag_aa_large_text_threshold_is_3_0():
    assert passes_wcag_aa(3.0, is_large_text=True) is True
    assert passes_wcag_aa(2.99, is_large_text=True) is False


# -------- Background-color estimation --------

def test_estimate_background_filters_text_pixels(tmp_path):
    """A 100x100 white image with a 20x20 navy square in the middle should
    estimate the background as ~white when we tell it the navy is the text."""
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    Image.new("RGB", (20, 20), (2, 62, 138)).copy()  # navy
    # Paste a navy square inside.
    inset = Image.new("RGB", (20, 20), (2, 62, 138))
    img.paste(inset, (40, 40))
    p = tmp_path / "img.png"
    img.save(p)

    bg = estimate_background_color(Image.open(p), (0, 0, 100, 100), text_color=(2, 62, 138))
    # Should be very close to pure white (a few navy pixels were filtered).
    assert min(bg) > 240


def test_estimate_background_falls_back_when_only_text(tmp_path):
    img = Image.new("RGB", (50, 50), (255, 255, 255))
    bg = estimate_background_color(img, (0, 0, 50, 50), text_color=(255, 255, 255))
    # Nothing escapes the text-color filter — function falls back to all pixels.
    assert bg == (255, 255, 255)


# -------- ContrastRule on synthetic images --------

def _brand_with_qc() -> BrandGuidelines:
    with open("inputs/brand/guidelines.yaml") as f:
        return BrandGuidelines.model_validate(yaml.safe_load(f))


def test_contrast_rule_passes_on_white_text_on_black(tmp_path):
    img_path = tmp_path / "high.png"
    img = Image.new("RGB", (400, 200), (0, 0, 0))
    img.save(img_path)

    rule = ContrastRule(min_ratio=4.5)
    output_meta = {
        "path": str(img_path),
        "headline_box": [50, 50, 350, 150],
        "text_color_used": "#FFFFFF",
    }
    result = rule.check(output_meta, img, _brand_with_qc())
    assert result["passed"] is True
    assert result["severity"] == "info"
    assert result["details"]["wcag_level"] == "AAA"
    assert result["details"]["contrast_ratio"] >= 19.0


def test_contrast_rule_fails_on_navy_text_on_navy_bg(tmp_path):
    img_path = tmp_path / "low.png"
    img = Image.new("RGB", (400, 200), (10, 70, 140))  # near-navy bg
    img.save(img_path)

    rule = ContrastRule(min_ratio=4.5)
    output_meta = {
        "path": str(img_path),
        "headline_box": [50, 50, 350, 150],
        "text_color_used": "#023E8A",  # navy
    }
    result = rule.check(output_meta, img, _brand_with_qc())
    assert result["passed"] is False
    assert result["severity"] == "fail"
    assert result["details"]["wcag_level"] == "fail"
    assert "Contrast" in result["reason"]


def test_contrast_rule_missing_metadata_fails_with_reason(tmp_path):
    img_path = tmp_path / "x.png"
    Image.new("RGB", (10, 10)).save(img_path)
    rule = ContrastRule()
    # No headline_box or text_color_used.
    result = rule.check({"path": str(img_path)}, Image.open(img_path), _brand_with_qc())
    assert result["passed"] is False
    assert "Missing" in result["reason"]


def test_evaluate_contrast_returns_full_payload(tmp_path):
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    p = tmp_path / "x.png"
    img.save(p)
    payload = evaluate_contrast(str(p), (0, 0, 200, 200), "#FFFFFF")
    for k in ("headline_box", "text_color", "background_color", "contrast_ratio",
              "threshold", "wcag_level", "passed", "is_large_text"):
        assert k in payload


# -------- build_rules respects required_brand_checks --------

def test_build_rules_includes_contrast_when_enabled():
    brand = _brand_with_qc()
    assert brand.required_brand_checks.contrast_ratio is True
    rules = build_rules(brand)
    assert any(r.name == "contrast_ratio" for r in rules)


def test_build_rules_skips_contrast_when_disabled():
    brand = _brand_with_qc()
    custom = brand.model_copy(deep=True)
    custom.required_brand_checks.contrast_ratio = False
    rules = build_rules(custom)
    assert not any(r.name == "contrast_ratio" for r in rules)


# -------- QCCheckerAgent end-to-end --------

class _FakeCtx:
    def __init__(self, state: dict):
        self.session = SimpleNamespace(state=state)


def _drain(agent: QCCheckerAgent, state: dict):
    """Run until exhausted or until QCFailure is raised. Returns merged state delta."""
    async def go():
        last_delta: dict = {}
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                last_delta.update(event.actions.state_delta)
        return last_delta
    return asyncio.run(go())


def _state_with_one_output(brand: BrandGuidelines, brief_overrides: dict, image_path: str,
                            text_color: str, headline_box: list[int]) -> tuple[str, dict]:
    pid = "p1"
    brief = {
        "campaign_id": "c", "campaign_name": "C", "brand_id": brand.brand_id,
        "language": "en", "localized_copy": False, "localized_legal_copy": False,
        "force_generate_hero": False, "regenerate_cached_assets": False,
        "halt_on_qc_failure": False,
        "target_region": "LATAM", "markets": ["MX"], "target_audience": "x",
        "creative_quality": "demo_polished",
        "layout_template": next(iter(brand.layout_templates.keys())),
        "campaign_message": "Headline",
        "products": [{"id": pid, "name": "P", "category": "x", "description": "y"}],
        **brief_overrides,
    }
    state = {
        "brand": brand.model_dump(mode="json"),
        "brief": brief,
        f"product:{pid}": {
            "outputs": [{
                "path": image_path,
                "ratio": "1x1",
                "market": "MX",
                "headline_box": headline_box,
                "text_color_used": text_color,
            }],
        },
    }
    return pid, state


def test_qc_agent_passes_high_contrast_output(tmp_path):
    """White text on black background → QC pass."""
    img_path = tmp_path / "high.png"
    Image.new("RGB", (1080, 1080), (0, 0, 0)).save(img_path)
    brand = _brand_with_qc()
    pid, state = _state_with_one_output(
        brand, {}, str(img_path), "#FFFFFF",
        [64, 700, 1016, 850],
    )

    delta = _drain(QCCheckerAgent(name="QC", product_id=pid), state)
    qc = delta[f"product:{pid}"]["qc_check"]
    assert qc["summary"] == "pass"
    assert qc["failures"] == []
    out0 = delta[f"product:{pid}"]["outputs"][0]
    assert out0["qc_check"]["summary"] == "pass"
    assert out0["qc_check"]["wcag_level"] in {"AA", "AAA"}
    assert out0["qc_check"]["contrast_ratio"] >= 4.5


def test_qc_agent_fails_low_contrast_without_halt(tmp_path):
    """Navy on navy → fail recorded but pipeline does not raise."""
    img_path = tmp_path / "low.png"
    Image.new("RGB", (1080, 1080), (10, 70, 140)).save(img_path)
    brand = _brand_with_qc()
    pid, state = _state_with_one_output(
        brand, {"halt_on_qc_failure": False}, str(img_path), "#023E8A",
        [64, 700, 1016, 850],
    )

    delta = _drain(QCCheckerAgent(name="QC", product_id=pid), state)
    qc = delta[f"product:{pid}"]["qc_check"]
    assert qc["summary"] == "fail"
    assert len(qc["failures"]) == 1
    out0 = delta[f"product:{pid}"]["outputs"][0]
    assert out0["qc_check"]["summary"] == "fail"
    assert out0["qc_check"]["contrast_ratio"] < 4.5


def test_qc_agent_raises_when_halt_on_failure_true(tmp_path):
    """halt_on_qc_failure=true → QCFailure after recording failure to state."""
    img_path = tmp_path / "low.png"
    Image.new("RGB", (1080, 1080), (10, 70, 140)).save(img_path)
    brand = _brand_with_qc()
    pid, state = _state_with_one_output(
        brand, {"halt_on_qc_failure": True}, str(img_path), "#023E8A",
        [64, 700, 1016, 850],
    )

    with pytest.raises(QCFailure):
        _drain(QCCheckerAgent(name="QC", product_id=pid), state)


def test_qc_agent_skips_when_no_rules_enabled(tmp_path):
    img_path = tmp_path / "x.png"
    Image.new("RGB", (1080, 1080), (0, 0, 0)).save(img_path)
    brand = _brand_with_qc().model_copy(deep=True)
    brand.required_brand_checks.contrast_ratio = False
    pid, state = _state_with_one_output(
        brand, {}, str(img_path), "#FFFFFF", [64, 700, 1016, 850],
    )

    delta = _drain(QCCheckerAgent(name="QC", product_id=pid), state)
    assert delta[f"product:{pid}"]["qc_check"]["summary"] == "skipped"


# -------- Reporter surfaces QC fields --------

def test_report_includes_qc_fields(tmp_path, monkeypatch):
    """End-to-end: state pre-seeded with qc_check + per-output qc_check; the
    reporter must surface qc_check, contrast_ratio, wcag_level, qc_rules in
    the per-output row plus qc_check_summary on the product row."""
    from creative_pipeline.sub_agents.reporter.agent import ReportingAgent

    monkeypatch.chdir(tmp_path)
    pid = "p1"
    out_path = tmp_path / "out.png"
    Image.new("RGB", (200, 200), (0, 0, 0)).save(out_path)

    state = {
        "brief": {"campaign_id": "c", "language": "en", "force_generate_hero": True},
        "product_ids": [pid],
        f"product:{pid}": {
            "hero_path": "src.png",
            "asset_source": "openai_generated",
            "image_provider": "openai",
            "image_model": "gpt-image-1",
            "outputs": [{
                "path": str(out_path),
                "market": "MX", "locale": "en", "ratio": "1x1",
                "headline_box": [10, 10, 190, 190],
                "text_color_used": "#FFFFFF",
                "qc_check": {
                    "summary": "pass",
                    "contrast_ratio": 21.0,
                    "wcag_level": "AAA",
                    "text_color": "#FFFFFF",
                    "background_color": "#000000",
                    "headline_box": [10, 10, 190, 190],
                    "rules": [{"name": "contrast_ratio", "passed": True, "severity": "info",
                               "details": {}, "reason": "Contrast 21.0:1 passes WCAG AAA."}],
                },
            }],
            "brand_check": {"summary": "pass", "per_output": []},
            "legal_check": {"summary": "pass", "failures": []},
            "qc_check": {"summary": "pass", "failures": [], "rules_run": ["contrast_ratio"]},
        },
    }

    async def run():
        async for _ in ReportingAgent(name="R")._run_async_impl(_FakeCtx(state)):
            pass

    asyncio.run(run())
    reports = sorted(Path("outputs").glob("report_*.json"))
    assert reports
    report = json.loads(reports[-1].read_text())

    product = report["products"][0]
    assert product["qc_check_summary"] == "pass"
    assert product["qc_rules_run"] == ["contrast_ratio"]
    assert product["qc_failures"] == []

    out0 = product["outputs"][0]
    assert out0["qc_check"] == "pass"
    assert out0["contrast_ratio"] == 21.0
    assert out0["wcag_level"] == "AAA"
    assert out0["text_color"] == "#FFFFFF"
    assert out0["background_color"] == "#000000"
    assert isinstance(out0["qc_rules"], list)
    assert out0["qc_rules"][0]["name"] == "contrast_ratio"
