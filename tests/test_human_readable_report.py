"""Human-readable report (Markdown) + retention/cleanup CLI flags.

Covers the post-run reporting upgrades:
  - ReportingAgent writes BOTH ``report_<ts>.json`` AND ``report_<ts>.md``
  - ``latest_report.{json,md}`` convenience copies are refreshed
  - The Markdown renderer is a pure function (testable in isolation)
  - The Markdown reflects campaign / product / output / Dropbox info
  - The Dropbox agent regenerates the Markdown with the real upload result
  - ``--clean-outputs`` removes outputs/* but preserves ``.gitkeep``
  - ``--keep-reports N`` prunes old report pairs but keeps the newest N
  - The compact console summary surfaces report paths + statuses
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from creative_pipeline.sub_agents.reporter.agent import (
    ReportingAgent,
    _render_markdown_report,
    _summarize_dropbox_meta,
    _summarize_warnings,
    _worst_status,
    _write_report_pair,
)


# ---------- Test fixtures: a representative report dict --------------------


def _sample_report() -> dict:
    """A trimmed-down report dict mirroring the real reporter's output —
    enough fields to exercise the renderer end-to-end without loading
    11k-line real reports."""
    return {
        "campaign_id": "summer_refresh_2025",
        "campaign_name": "Summer Refresh 2025",
        "brand_id": "aquacorp_global",
        "started_at": "2026-05-07T18:30:00+00:00",
        "completed_at": "2026-05-07T18:31:30+00:00",
        "duration_ms": 90123,
        "language": "en",
        "localized_copy": False,
        "localized_legal_copy": False,
        "force_generate_hero": True,
        "regenerate_cached_assets": True,
        "creative_quality": "demo_polished",
        "layout_template": "premium_product_hero",
        "provider_env": "openai",
        "model_env": "gpt-5",
        "image_provider_env": "openai",
        "products": [
            {
                "product_id": "aquavita_sparkling",
                "asset_source": "openai_generated",
                "source_asset_path": "outputs/aquavita_sparkling/source/global_x.png",
                "source_origin": "generated_this_run",
                "image_provider": "openai",
                "image_model": "gpt-image-1",
                "image_gen_latency_ms": 45055,
                "used_cache": False,
                "brand_check_summary": "warn",
                "legal_check_summary": "pass",
                "qc_check_summary": "pass",
                "qc_failures": [],
                "warnings": [],
                "outputs": [
                    {
                        "market": "MX",
                        "locale": "en",
                        "ratio": "1x1",
                        "path": "outputs/aquavita_sparkling/1x1/MX_en.png",
                        "brand_check": "warn",
                        "brand_check_reason": "Photography palette diverges from brand colors",
                        "legal_check": "pass",
                        "qc_check": "pass",
                        "wcag_level": "AA",
                        "disclaimer_wcag_level": "AA",
                        "composition_score": 0.92,
                        "composition_warnings": [],
                        "headline_size_px": 60,
                        "headline_line_count": 2,
                        "headline_color_selected": "#FFFFFF",
                        "headline_box_selected_pct": [0.07, 0.20, 0.45, 0.50],
                        "headline_prominence_score": 0.85,
                        "logo_position_selected": "top-right",
                        "logo_position_configured": "top-right",
                        "logo_position_adjusted": False,
                        "logo_product_gap_px": 122,
                        "disclaimer_position_selected": "candidate_0",
                        "disclaimer_contrast_ratio": 4.7,
                        "contrast_ratio": 5.4,
                    },
                    {
                        "market": "BR",
                        "locale": "en",
                        "ratio": "9x16",
                        "path": "outputs/aquavita_sparkling/9x16/BR_en.png",
                        "brand_check": "warn",
                        "legal_check": "pass",
                        "qc_check": "pass",
                        "wcag_level": "AA-large",
                        "disclaimer_wcag_level": "AA",
                        "composition_score": 0.88,
                        "composition_warnings": ["focal_edge_clip"],
                        "headline_size_px": 94,
                        "headline_line_count": 4,
                        "headline_color_selected": "#023E8A",
                        "headline_prominence_score": 0.74,
                        "logo_position_selected": "top-left",
                        "logo_position_configured": "top-right",
                        "logo_position_adjusted": True,
                        "logo_product_gap_px": 308,
                        "disclaimer_position_selected": "candidate_1",
                        "disclaimer_contrast_ratio": 4.5,
                        "contrast_ratio": 3.5,
                    },
                ],
            },
            {
                "product_id": "sunguard_spf50",
                "source_origin": "generated_this_run",
                "image_provider": "openai",
                "image_model": "gpt-image-1",
                "used_cache": False,
                "brand_check_summary": "pass",
                "legal_check_summary": "pass",
                "qc_check_summary": "pass",
                "qc_failures": [],
                "warnings": [],
                "outputs": [
                    {
                        "market": "MX",
                        "locale": "en",
                        "ratio": "16x9",
                        "path": "outputs/sunguard_spf50/16x9/MX_en.png",
                        "brand_check": "pass",
                        "legal_check": "pass",
                        "qc_check": "pass",
                        "wcag_level": "AAA",
                        "composition_score": 0.95,
                        "composition_warnings": [],
                        "headline_size_px": 96,
                        "headline_line_count": 2,
                        "contrast_ratio": 7.5,
                    },
                ],
            },
        ],
    }


# ---------- 1. Renderer pure-function tests ------------------------------


def test_renderer_includes_run_summary_block():
    md = _render_markdown_report(_sample_report())
    assert "# Creative Pipeline Run Report" in md
    assert "## Run Summary" in md
    assert "Summer Refresh 2025" in md
    assert "summer_refresh_2025" in md
    assert "aquacorp_global" in md
    assert "premium_product_hero" in md
    assert "demo_polished" in md
    assert "openai" in md
    assert "gpt-5" in md


def test_renderer_includes_overall_status_table():
    md = _render_markdown_report(_sample_report())
    assert "## Overall Status" in md
    assert "| Brand |" in md
    assert "| Legal |" in md
    assert "| QC |" in md
    # warn for brand because aquavita_sparkling is "warn"
    assert "warn" in md.lower()


def test_renderer_includes_per_product_sections():
    md = _render_markdown_report(_sample_report())
    assert "### `aquavita_sparkling`" in md
    assert "### `sunguard_spf50`" in md
    # Per-product table headers — Locale column sits between Market and Ratio
    # so reviewers don't have to parse the locale out of the filename.
    assert "| Market | Locale | Ratio | Output | Brand | Legal | QC |" in md
    # Output row shows up
    assert "outputs/aquavita_sparkling/1x1/MX_en.png" in md
    assert "outputs/sunguard_spf50/16x9/MX_en.png" in md


def test_renderer_per_product_table_populates_locale_column():
    """Every output row's Locale cell shows the resolved locale."""
    report = _sample_report()
    # Set distinct locales so we can verify the cell content.
    report["products"][0]["outputs"][0]["locale"] = "es"
    report["products"][0]["outputs"][1]["locale"] = "pt"
    report["products"][1]["outputs"][0]["locale"] = "en"
    md = _render_markdown_report(report)
    # The cell shows up next to the market column, e.g. "| MX | es | 1x1 |".
    assert "| MX | es | 1x1 |" in md
    assert "| BR | pt | 9x16 |" in md
    assert "| MX | en | 16x9 |" in md


def test_renderer_summary_says_locales_when_output_locales_set():
    """When report carries output_locales, the count line uses 'locales'
    wording and quotes the configured list."""
    report = _sample_report()
    report["output_locales"] = ["en", "es", "pt"]
    report["localized_copy"] = True
    md = _render_markdown_report(report)
    # Final creatives produced line mentions locales count + the explicit list.
    assert "locale" in md.lower()  # "× N locales" in the math line
    assert "output_locales=" in md or "output_locales`" in md or "['en', 'es', 'pt']" in md


def test_renderer_summary_count_math_under_output_locales():
    """The 'Final creatives produced' multiplication breakdown should use
    the actual market/locale/ratio shape — not collapse locales into the
    market count."""
    # 1 market × 3 locales × 2 ratios × 2 products = 12 outputs.
    report = {
        "campaign_id": "x", "campaign_name": "X", "brand_id": "b",
        "started_at": "2026-05-07T00:00:00+00:00",
        "completed_at": "2026-05-07T00:01:00+00:00",
        "duration_ms": 60000,
        "language": "en", "localized_copy": True, "localized_legal_copy": True,
        "output_locales": ["en", "es", "pt"],
        "creative_quality": "demo_polished",
        "layout_template": "premium_product_hero",
        "products": [
            {
                "product_id": f"p{i}", "outputs": [
                    {"market": "US", "locale": loc, "ratio": ratio,
                     "path": f"outputs/p{i}/{ratio}/US_{loc}.png",
                     "brand_check": "pass", "legal_check": "pass", "qc_check": "pass"}
                    for ratio in ("1x1", "9x16")
                    for loc in ("en", "es", "pt")
                ],
                "brand_check_summary": "pass", "legal_check_summary": "pass",
                "qc_check_summary": "pass", "qc_failures": [], "warnings": [],
            }
            for i in (1, 2)
        ],
    }
    md = _render_markdown_report(report)
    # Total = 12; breakdown should mention 2 products, 2 ratios, 1 market, 3 locales.
    assert "12" in md
    assert "2 products" in md
    assert "2 aspect ratios" in md
    assert "1 market" in md
    assert "3 locales" in md


def test_renderer_includes_composition_notes_compact():
    md = _render_markdown_report(_sample_report())
    assert "## Composition Notes" in md
    assert "Headline:" in md
    assert "Logo:" in md
    assert "Disclaimer:" in md
    # The verbose internals (every candidate score, every shift attempt)
    # must NOT appear in the human-readable view.
    assert "headline_box_scores" not in md
    assert "logo_position_attempts" not in md
    assert "crop_box_scores" not in md
    assert "qc_rules" not in md  # full rule details


def test_renderer_collects_failures_and_warnings():
    md = _render_markdown_report(_sample_report())
    assert "## Failures and Warnings" in md
    # Brand is warn → composition_warning is focal_edge_clip → both appear.
    assert "[brand:aquavita_sparkling]" in md
    assert "[composition:aquavita_sparkling]" in md
    assert "focal_edge_clip" in md


def test_renderer_says_no_failures_when_clean():
    clean_report = _sample_report()
    for product in clean_report["products"]:
        product["brand_check_summary"] = "pass"
        for output in product["outputs"]:
            output["brand_check"] = "pass"
            output["composition_warnings"] = []
    md = _render_markdown_report(clean_report)
    assert "No blocking failures." in md


def test_renderer_dropbox_disabled_when_meta_says_disabled():
    md = _render_markdown_report(
        _sample_report(),
        dropbox_meta={"enabled": False, "reason": "disabled"},
    )
    assert "disabled" in md.lower()


def test_renderer_dropbox_includes_folder_when_uploaded():
    md = _render_markdown_report(
        _sample_report(),
        dropbox_meta={
            "enabled": True,
            "dropbox_run_folder": "/runs/summer_refresh_2025/20260507T184413Z",
            "uploaded_count": 23,
            "failures": 0,
        },
    )
    assert "/runs/summer_refresh_2025/20260507T184413Z" in md
    assert "uploaded 23 files" in md


def test_renderer_dropbox_invalid_token_surfaces_error():
    """When the Dropbox preflight fails (invalid token / missing scope),
    the Markdown should call this out so a reviewer doesn't have to
    inspect the agent log."""
    md = _render_markdown_report(
        _sample_report(),
        dropbox_meta={
            "enabled": True,
            "reason": "invalid_token",
            "error": "AuthError: invalid_access_token",
        },
    )
    assert "skipped" in md.lower()
    assert "invalid_token" in md


# ---------- 2. Helper functions ------------------------------------------


def test_worst_status_picks_fail_over_warn_over_pass():
    assert _worst_status(["pass", "warn", "fail"]) == "fail"
    assert _worst_status(["pass", "warn"]) == "warn"
    assert _worst_status(["pass", "pass"]) == "pass"
    assert _worst_status([]) == "n/a"
    assert _worst_status([None, None]) == "n/a"


def test_summarize_dropbox_meta_classifies_correctly():
    # No meta + env unset → not run
    import os
    if "DROPBOX_UPLOAD_ENABLED" in os.environ:
        prev = os.environ.pop("DROPBOX_UPLOAD_ENABLED")
    else:
        prev = None
    try:
        status, _ = _summarize_dropbox_meta(None)
        assert status == "not run"
    finally:
        if prev is not None:
            os.environ["DROPBOX_UPLOAD_ENABLED"] = prev

    # Disabled by agent
    status, _ = _summarize_dropbox_meta({"enabled": False, "reason": "disabled"})
    assert status == "not run"

    # Successful upload
    status, _ = _summarize_dropbox_meta({
        "enabled": True, "dropbox_run_folder": "/x", "uploaded_count": 5,
        "failures": 0,
    })
    assert status == "pass"

    # Partial failure
    status, _ = _summarize_dropbox_meta({
        "enabled": True, "dropbox_run_folder": "/x", "uploaded_count": 3,
        "failures": 2,
    })
    assert status == "warn"

    # Total failure (scope missing)
    status, _ = _summarize_dropbox_meta({
        "enabled": True, "dropbox_run_folder": "/x", "uploaded_count": 0,
        "failures": 5,
    })
    assert status == "fail"

    # Skipped before upload (e.g. invalid_token)
    status, _ = _summarize_dropbox_meta({
        "enabled": True, "reason": "invalid_token", "error": "..."
    })
    assert status == "fail"


def test_summarize_warnings_collects_per_source():
    issues = _summarize_warnings(_sample_report())
    sources = {i.split(" ", 1)[0] for i in issues}
    # Should see brand warnings (aquavita_sparkling brand=warn) and
    # composition warnings (BR/9x16 has focal_edge_clip).
    assert any("brand:" in s for s in sources)
    assert any("composition:" in s for s in sources)


# ---------- 3. _write_report_pair end-to-end on disk ---------------------


def test_write_report_pair_writes_json_and_md(tmp_path):
    report = _sample_report()
    json_path, md_path = _write_report_pair(
        output_dir=str(tmp_path),
        timestamp="20260507T184413Z",
        report=report,
    )
    assert Path(json_path).exists()
    assert Path(md_path).exists()
    # JSON content matches the dict.
    parsed = json.loads(Path(json_path).read_text())
    assert parsed["campaign_id"] == "summer_refresh_2025"
    # Markdown is human-readable.
    md = Path(md_path).read_text()
    assert "# Creative Pipeline Run Report" in md
    assert "summer_refresh_2025" in md


def test_write_report_pair_refreshes_latest_copies(tmp_path):
    report = _sample_report()
    _write_report_pair(
        output_dir=str(tmp_path),
        timestamp="20260507T184413Z",
        report=report,
    )
    latest_json = tmp_path / "latest_report.json"
    latest_md = tmp_path / "latest_report.md"
    assert latest_json.exists()
    assert latest_md.exists()
    # Latest copies have the same content as the timestamped versions.
    assert latest_json.read_text() == (tmp_path / "report_20260507T184413Z.json").read_text()
    assert latest_md.read_text() == (tmp_path / "report_20260507T184413Z.md").read_text()


def test_write_report_pair_overwrites_latest_on_subsequent_run(tmp_path):
    """A second run must replace ``latest_report.{json,md}`` with the
    newer run's content, not append."""
    _write_report_pair(
        output_dir=str(tmp_path),
        timestamp="20260101T000000Z",
        report={**_sample_report(), "campaign_id": "first_campaign"},
    )
    _write_report_pair(
        output_dir=str(tmp_path),
        timestamp="20260202T000000Z",
        report={**_sample_report(), "campaign_id": "second_campaign"},
    )
    latest = json.loads((tmp_path / "latest_report.json").read_text())
    assert latest["campaign_id"] == "second_campaign"


def test_write_report_pair_includes_dropbox_meta_when_supplied(tmp_path):
    """When called with ``dropbox_meta`` (i.e. by the DropboxUploaderAgent
    after upload completes), the Markdown must reflect the actual upload."""
    _write_report_pair(
        output_dir=str(tmp_path),
        timestamp="20260507T184413Z",
        report=_sample_report(),
        dropbox_meta={
            "enabled": True,
            "dropbox_run_folder": "/runs/summer_refresh_2025/20260507T184413Z",
            "uploaded_count": 23,
            "failures": 0,
        },
        dropbox_manifest_path=str(tmp_path / "dropbox_upload_20260507T184413Z.json"),
    )
    md = (tmp_path / "report_20260507T184413Z.md").read_text()
    assert "/runs/summer_refresh_2025/20260507T184413Z" in md
    assert "uploaded 23 files" in md


# ---------- 4. ReportingAgent in-pipeline writes both files --------------


class _FakeCtx:
    def __init__(self, state: dict):
        class _Sess:
            def __init__(self, s): self.state = s
        self.session = _Sess(state)


def test_reporter_agent_writes_json_and_md_and_state_carries_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    state = {
        "brief": {
            "campaign_id": "summer_refresh_2025",
            "campaign_name": "Summer Refresh",
            "brand_id": "aquacorp_global",
            "language": "en",
            "localized_copy": False,
            "localized_legal_copy": False,
            "creative_quality": "demo_polished",
            "layout_template": "premium_product_hero",
        },
        "product_ids": ["p1"],
        "product:p1": {
            "outputs": [
                {
                    "market": "MX", "ratio": "1x1", "locale": "en",
                    "path": str(tmp_path / "p1" / "1x1" / "MX_en.png"),
                    "headline_size_px": 56, "headline_line_count": 2,
                    "composition_score": 0.9,
                    "qc_check": {"summary": "pass"},
                },
            ],
            "brand_check": {"summary": "pass", "per_output": []},
            "legal_check": {"summary": "pass"},
            "qc_check": {"summary": "pass"},
        },
    }

    async def go():
        deltas: dict = {}
        async for event in ReportingAgent(name="r")._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                deltas.update(event.actions.state_delta)
        return deltas

    delta = asyncio.run(go())
    assert "report_path" in delta
    assert "markdown_report_path" in delta
    json_path = Path(delta["report_path"])
    md_path = Path(delta["markdown_report_path"])
    assert json_path.exists()
    assert md_path.exists()
    assert (tmp_path / "latest_report.json").exists()
    assert (tmp_path / "latest_report.md").exists()
    # The Markdown reflects the same campaign metadata.
    md = md_path.read_text()
    assert "summer_refresh_2025" in md


# ---------- 5. CLI: --clean-outputs ---------------------------------------


def _run_pipeline_help_subprocess() -> subprocess.CompletedProcess:
    """Helper: spawn ``scripts/run_pipeline.py --help`` to verify CLI args
    without having to mock the full ADK pipeline."""
    repo_root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "scripts/run_pipeline.py", "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(repo_root),
    )


def test_run_pipeline_advertises_new_flags():
    result = _run_pipeline_help_subprocess()
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "--clean-outputs" in out
    assert "--keep-reports" in out


def test_clean_outputs_dir_preserves_gitkeep(tmp_path):
    from scripts.run_pipeline import _clean_outputs_dir

    out = tmp_path / "outputs"
    out.mkdir()
    (out / ".gitkeep").touch()
    (out / "report_20260101T000000Z.json").write_text("{}")
    (out / "aquavita_sparkling").mkdir()
    (out / "aquavita_sparkling" / "1x1").mkdir()
    (out / "aquavita_sparkling" / "1x1" / "MX_en.png").write_bytes(b"")

    removed = _clean_outputs_dir(out)
    assert removed >= 2  # at least the report file + the product dir
    assert (out / ".gitkeep").exists()
    assert not (out / "report_20260101T000000Z.json").exists()
    assert not (out / "aquavita_sparkling").exists()


def test_clean_outputs_dir_creates_dir_if_missing(tmp_path):
    from scripts.run_pipeline import _clean_outputs_dir
    target = tmp_path / "fresh_outputs"
    assert not target.exists()
    _clean_outputs_dir(target)
    assert target.exists()
    assert (target / ".gitkeep").exists()


# ---------- 6. CLI: --keep-reports N --------------------------------------


def test_keep_reports_keeps_only_newest_n(tmp_path):
    from scripts.run_pipeline import _prune_old_reports

    out = tmp_path / "outputs"
    out.mkdir()
    (out / ".gitkeep").touch()
    timestamps = [
        "20260101T000000Z",  # oldest
        "20260202T000000Z",
        "20260303T000000Z",
        "20260404T000000Z",  # newest
    ]
    for ts in timestamps:
        (out / f"report_{ts}.json").write_text("{}")
        (out / f"report_{ts}.md").write_text("# old")
        (out / f"dropbox_upload_{ts}.json").write_text("{}")
    # Latest convenience copies must NOT be touched.
    (out / "latest_report.json").write_text("{}")
    (out / "latest_report.md").write_text("# latest")

    pruned = _prune_old_reports(out, keep=2)
    # The two newest survive.
    assert (out / f"report_{timestamps[-1]}.json").exists()
    assert (out / f"report_{timestamps[-2]}.json").exists()
    # Older pairs (json + md + dropbox_upload manifest) are gone.
    for ts in timestamps[:-2]:
        assert not (out / f"report_{ts}.json").exists()
        assert not (out / f"report_{ts}.md").exists()
        assert not (out / f"dropbox_upload_{ts}.json").exists()
    # Latest copies untouched.
    assert (out / "latest_report.json").exists()
    assert (out / "latest_report.md").exists()
    # Result shape is correct.
    assert len(pruned["kept"]) == 2
    assert len(pruned["removed"]) == 2


def test_keep_reports_zero_removes_all_timestamped_pairs(tmp_path):
    from scripts.run_pipeline import _prune_old_reports
    out = tmp_path / "outputs"
    out.mkdir()
    for ts in ("20260101T000000Z", "20260202T000000Z"):
        (out / f"report_{ts}.json").write_text("{}")
        (out / f"report_{ts}.md").write_text("")
    pruned = _prune_old_reports(out, keep=0)
    assert pruned["removed"] == ["20260101T000000Z", "20260202T000000Z"]
    assert list(out.glob("report_*.json")) == []


def test_keep_reports_large_n_no_op(tmp_path):
    from scripts.run_pipeline import _prune_old_reports
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "report_20260101T000000Z.json").write_text("{}")
    pruned = _prune_old_reports(out, keep=99)
    assert pruned["removed"] == []
    assert (out / "report_20260101T000000Z.json").exists()


def test_keep_reports_ignores_unrelated_files(tmp_path):
    """Only ``report_<ts>.json`` files match the prune target. Stray
    files like ``rerender_summary.json`` or unrelated timestamps should
    survive untouched."""
    from scripts.run_pipeline import _prune_old_reports
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "report_20260101T000000Z.json").write_text("{}")
    (out / "report_20260101T000000Z.md").write_text("")
    (out / "rerender_summary.json").write_text("{}")
    (out / "some_random_file.txt").write_text("")
    _prune_old_reports(out, keep=0)
    assert (out / "rerender_summary.json").exists()
    assert (out / "some_random_file.txt").exists()
    assert not (out / "report_20260101T000000Z.json").exists()


# ---------- 7. Compact console summary ------------------------------------


def test_compact_summary_includes_report_paths_and_statuses(tmp_path, capsys, monkeypatch):
    """Build a fake final-state + a real JSON report on disk, then call
    ``_print_compact_summary`` and assert the output contains the four
    things a stakeholder cares about: JSON path, MD path, Dropbox status,
    overall pass/warn/fail."""
    from scripts.run_pipeline import _print_compact_summary, _parse_args

    # Write a minimal report with one product so the summary can read it.
    report = {
        "campaign_id": "test", "campaign_name": "Test", "brand_id": "x",
        "products": [
            {
                "product_id": "p1",
                "brand_check_summary": "warn",
                "legal_check_summary": "pass",
                "qc_check_summary": "pass",
                "outputs": [{"market": "MX", "ratio": "1x1"}],
            },
        ],
    }
    json_path = tmp_path / "report_20260101T000000Z.json"
    json_path.write_text(json.dumps(report))
    md_path = tmp_path / "report_20260101T000000Z.md"
    md_path.write_text("# md")

    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    final_state = {
        "report_path": str(json_path),
        "markdown_report_path": str(md_path),
        "dropbox_upload_meta": {
            "enabled": True,
            "dropbox_run_folder": "/runs/test/20260101T000000Z",
            "uploaded_count": 5,
            "failures": 0,
        },
    }
    args = _parse_args([])
    _print_compact_summary(args, final_state, pruned=None)
    captured = capsys.readouterr()
    out = captured.out
    assert "Run complete." in out
    assert str(json_path) in out
    assert str(md_path) in out
    assert "uploaded 5 files" in out
    assert "Brand: warn" in out
    assert "Legal: pass" in out
    assert "QC:    pass" in out
    assert "p1" in out


def test_compact_summary_handles_no_dropbox(tmp_path, capsys, monkeypatch):
    from scripts.run_pipeline import _print_compact_summary, _parse_args
    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    json_path = tmp_path / "report.json"
    json_path.write_text(json.dumps({"products": []}))
    final_state = {"report_path": str(json_path), "markdown_report_path": "x.md"}
    args = _parse_args([])
    _print_compact_summary(args, final_state, pruned=None)
    out = capsys.readouterr().out
    assert "not enabled" in out


def test_compact_summary_handles_skipped_dropbox(tmp_path, capsys, monkeypatch):
    """When upload was enabled but skipped (e.g. invalid_token), the
    summary surfaces the reason rather than fake-success."""
    from scripts.run_pipeline import _print_compact_summary, _parse_args
    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    json_path = tmp_path / "report.json"
    json_path.write_text(json.dumps({"products": []}))
    final_state = {
        "report_path": str(json_path),
        "markdown_report_path": "x.md",
        "dropbox_upload_meta": {
            "enabled": True, "reason": "invalid_token",
            "error": "AuthError",
        },
    }
    args = _parse_args([])
    _print_compact_summary(args, final_state, pruned=None)
    out = capsys.readouterr().out
    assert "skipped" in out
    assert "invalid_token" in out
