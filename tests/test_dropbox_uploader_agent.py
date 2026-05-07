"""DropboxUploaderAgent — final-step agent in the root_agent graph.

These tests confirm the agent's contract with the rest of the pipeline:
  - When DROPBOX_UPLOAD_ENABLED is unset/false, the agent yields a "skipped"
    event and does NOT import the dropbox SDK or touch the network.
  - When enabled but no report exists in state, the agent skips with a
    structured reason instead of raising.
  - When enabled and the upload succeeds, the agent writes the local
    manifest, surfaces a summary in state["dropbox_upload_meta"], and
    yields a single human-readable text event.
  - When the underlying uploader raises a config error (missing token /
    SDK), the agent records the error in state and never raises into the
    agent graph (an in-flight ADK run must not be aborted by Dropbox).

The actual Dropbox SDK is replaced by a FakeDropbox in
``test_dropbox_uploader.py``; here we monkey-patch
``creative_pipeline.tools.dropbox_uploader.upload_run_outputs`` to a
fake function so we don't even need the SDK installed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from creative_pipeline.sub_agents.dropbox_uploader.agent import (
    DropboxUploaderAgent,
    dropbox_uploader_agent,
)


class _FakeCtx:
    """Minimal stand-in for InvocationContext."""
    def __init__(self, state: dict):
        class _Session:
            def __init__(self, s): self.state = s
        self.session = _Session(state)


def _drain(state: dict) -> tuple[dict, list[str]]:
    """Run the agent once; return (final_state_delta, [event_text]).

    The agent emits exactly one event; we still collect the list to
    catch any future regression that yields multiple events."""
    agent = DropboxUploaderAgent(name="test_dropbox_uploader")
    async def go():
        delta: dict = {}
        texts: list[str] = []
        async for event in agent._run_async_impl(_FakeCtx(state)):
            if event.actions and event.actions.state_delta:
                delta.update(event.actions.state_delta)
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if getattr(part, "text", None):
                        texts.append(part.text)
        return delta, texts
    return asyncio.run(go())


def _write_report(tmp_path: Path, ts: str, campaign_id: str | None) -> Path:
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    report = out / f"report_{ts}.json"
    payload = {"campaign_id": campaign_id} if campaign_id else {}
    report.write_text(json.dumps(payload))
    return report


def _stub_token_validation_ok(monkeypatch) -> None:
    """Bypass the agent's pre-flight token validation so tests focused
    on later logic don't have to set up a fake Dropbox client just to
    answer ``users_get_current_account``. Tests that specifically exercise
    the validator path patch this themselves with a different return."""
    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.validate_dropbox_token",
        lambda **kwargs: (True, "valid (test stub)"),
    )


# ---------- 1. disabled by default ------------------------------------------


def test_agent_skips_when_dropbox_upload_enabled_unset(monkeypatch):
    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    delta, texts = _drain({"report_path": "/anything"})
    assert delta["dropbox_upload_meta"]["enabled"] is False
    assert delta["dropbox_upload_meta"]["reason"] == "disabled"
    assert any("skipped" in t.lower() for t in texts)


def test_agent_skips_when_dropbox_upload_enabled_false(monkeypatch):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "false")
    delta, _ = _drain({"report_path": "/anything"})
    assert delta["dropbox_upload_meta"]["enabled"] is False


def test_agent_does_not_import_dropbox_sdk_when_disabled(monkeypatch):
    """The agent's lazy-import path is the contract that keeps the SDK
    out of every default install. Verify by removing dropbox from
    sys.modules and asserting it stays gone after a disabled run."""
    import sys
    monkeypatch.delenv("DROPBOX_UPLOAD_ENABLED", raising=False)
    sys.modules.pop("dropbox", None)
    _drain({"report_path": "/anything"})
    assert "dropbox" not in sys.modules


# ---------- 2. enabled but pipeline incomplete -----------------------------


def test_agent_skips_when_no_report_path(monkeypatch):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    delta, texts = _drain({})  # no report_path in state
    meta = delta["dropbox_upload_meta"]
    assert meta["enabled"] is True
    assert meta["reason"] == "no_report_path"
    assert any("no report_path" in t for t in texts)


def test_agent_skips_when_report_has_no_campaign_id(monkeypatch, tmp_path):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.chdir(tmp_path)
    report = _write_report(tmp_path, "20260101T000000Z", campaign_id=None)
    delta, texts = _drain({"report_path": str(report)})
    meta = delta["dropbox_upload_meta"]
    assert meta["enabled"] is True
    assert meta["reason"] == "no_campaign_id"


# ---------- 3. enabled + uploader succeeds ---------------------------------


def test_agent_uploads_and_writes_manifest_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    # Self-contained: override DROPBOX_ROOT_FOLDER explicitly so .env's
    # value (whatever the developer's local Dropbox app expects) doesn't
    # leak into this test.
    monkeypatch.setenv("DROPBOX_ROOT_FOLDER", "/TD-Creative-Pipeline-POC")
    monkeypatch.chdir(tmp_path)
    _stub_token_validation_ok(monkeypatch)
    report = _write_report(tmp_path, "20260101T000000Z", campaign_id="cid")

    fake_result = {
        "dropbox_run_folder": "/TD-Creative-Pipeline-POC/cid/20260101T000000Z",
        "uploaded_count": 5,
        "uploaded_files": [{"local": "outputs/x.png", "dropbox": "/TD-.../x.png"}],
        "failures": [],
        "shared_links": [],
        "shared_link_failures": [],
        "manifest_path": None,
    }

    def _fake_upload(**kwargs):
        # Confirm the agent passes the right args through.
        assert kwargs["campaign_id"] == "cid"
        assert kwargs["run_timestamp"] == "20260101T000000Z"
        assert kwargs["dropbox_root"] == "/TD-Creative-Pipeline-POC"
        assert kwargs["create_shared_links"] is False
        return fake_result

    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.upload_run_outputs",
        _fake_upload,
    )

    delta, texts = _drain({"report_path": str(report)})

    meta = delta["dropbox_upload_meta"]
    assert meta["enabled"] is True
    assert meta["uploaded_count"] == 5
    assert meta["dropbox_run_folder"] == "/TD-Creative-Pipeline-POC/cid/20260101T000000Z"
    assert meta["failures"] == 0

    # Manifest written next to outputs/.
    manifest_path = tmp_path / "outputs" / "dropbox_upload_20260101T000000Z.json"
    assert manifest_path.exists()
    parsed = json.loads(manifest_path.read_text())
    assert parsed["campaign_id"] == "cid"
    assert parsed["uploaded_count"] == 5

    # Summary text on the event for `adk web` log viewers.
    assert any("Dropbox upload" in t and "5 files" in t for t in texts)


def test_agent_passes_shared_links_flag_through(monkeypatch, tmp_path):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DROPBOX_CREATE_SHARED_LINKS", "true")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.chdir(tmp_path)
    _stub_token_validation_ok(monkeypatch)
    report = _write_report(tmp_path, "20260101T000000Z", campaign_id="cid")

    captured: dict = {}

    def _fake_upload(**kwargs):
        captured.update(kwargs)
        return {
            "dropbox_run_folder": "/TD-Creative-Pipeline-POC/cid/20260101T000000Z",
            "uploaded_count": 0, "uploaded_files": [],
            "failures": [], "shared_links": [], "shared_link_failures": [],
            "manifest_path": None,
        }

    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.upload_run_outputs",
        _fake_upload,
    )

    _drain({"report_path": str(report)})
    assert captured["create_shared_links"] is True


def test_agent_passes_custom_dropbox_root_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DROPBOX_ROOT_FOLDER", "/Custom/Folder")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.chdir(tmp_path)
    _stub_token_validation_ok(monkeypatch)
    report = _write_report(tmp_path, "20260101T000000Z", campaign_id="cid")

    captured: dict = {}

    def _fake_upload(**kwargs):
        captured.update(kwargs)
        return {
            "dropbox_run_folder": "/Custom/Folder/cid/20260101T000000Z",
            "uploaded_count": 0, "uploaded_files": [],
            "failures": [], "shared_links": [], "shared_link_failures": [],
            "manifest_path": None,
        }

    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.upload_run_outputs",
        _fake_upload,
    )

    _drain({"report_path": str(report)})
    assert captured["dropbox_root"] == "/Custom/Folder"


# ---------- 4. enabled + uploader hits config error -----------------------


def test_agent_records_config_error_without_raising(monkeypatch, tmp_path):
    """Missing token / missing SDK / other config errors come out of the
    uploader as RuntimeError. The agent must catch + record, never raise
    into the agent graph (would abort the run for a non-essential step)."""
    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.chdir(tmp_path)
    _stub_token_validation_ok(monkeypatch)
    report = _write_report(tmp_path, "20260101T000000Z", campaign_id="cid")

    def _raising(**kwargs):
        raise RuntimeError("DROPBOX_ACCESS_TOKEN is required to upload to Dropbox.")

    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.upload_run_outputs",
        _raising,
    )

    delta, texts = _drain({"report_path": str(report)})

    meta = delta["dropbox_upload_meta"]
    assert meta["enabled"] is True
    assert meta["reason"] == "config_error"
    assert "DROPBOX_ACCESS_TOKEN" in meta["error"]
    assert any("upload failed" in t.lower() for t in texts)


# ---------- 5. agent is wired into root_agent -----------------------------


def test_root_agent_includes_dropbox_uploader_after_reporter():
    """Regression guard: DropboxUploaderAgent must run AFTER ReportingAgent
    (it depends on state["report_path"]) and must be the LAST step."""
    from creative_pipeline.agent import root_agent
    names = [a.name for a in root_agent.sub_agents]
    assert "DropboxUploaderAgent" in names
    # It must come after ReportingAgent.
    assert names.index("DropboxUploaderAgent") > names.index("ReportingAgent")
    # And it must be the final step.
    assert names[-1] == "DropboxUploaderAgent"


def test_module_exports_singleton():
    """Other sub_agents follow the `<name>_agent` singleton convention;
    keep that consistent for grep-ability."""
    assert dropbox_uploader_agent.name == "DropboxUploaderAgent"
