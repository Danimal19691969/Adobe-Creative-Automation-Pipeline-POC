"""DropboxUploaderAgent — optional final step in the agent graph.

Runs AFTER ``ReportingAgent`` so the report file exists on disk and the
state already carries ``state["report_path"]``. The agent is a no-op
unless ``DROPBOX_UPLOAD_ENABLED`` is truthy in the environment, so a
default `adk web` / `scripts/run_pipeline.py` run with no Dropbox
config behaves exactly as it did before this agent existed.

When upload is enabled, the agent:
  1. Resolves ``campaign_id`` + ``run_timestamp`` from ``state["report_path"]``.
  2. Calls ``upload_run_outputs`` (lazy import — the dropbox SDK is an
     optional ``[upload]`` extra; users who don't enable upload don't pay
     the import cost).
  3. Writes a local ``outputs/dropbox_upload_<run_timestamp>.json`` manifest.
  4. Yields a single summary event with the upload meta in
     ``state["dropbox_upload_meta"]``.

**Critical invariant:** the agent NEVER raises. A missing token, missing
SDK, network outage, per-file API error — all flow into the manifest /
summary event so a Dropbox problem can never abort an in-flight ADK run.
The reporter has already written its JSON; the rest of the pipeline is
already complete; this is strictly a snapshot side-effect.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

logger = logging.getLogger(__name__)


_DEFAULT_DROPBOX_ROOT = "/TD-Creative-Pipeline-POC"
_REPORT_TS_RE = re.compile(r"report_(\d{8}T\d{6}Z)\.json$")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _extract_run_timestamp_and_campaign(report_path: str) -> tuple[str, str | None]:
    """Pull ``run_timestamp`` from the report filename and ``campaign_id``
    from inside the JSON. Falls back to "now" if either fails so an upload
    is still possible when the report is malformed."""
    filename = Path(report_path).name
    m = _REPORT_TS_RE.search(filename)
    run_timestamp = (
        m.group(1) if m
        else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    campaign_id: str | None = None
    try:
        report_data = json.loads(Path(report_path).read_text())
        campaign_id = report_data.get("campaign_id")
    except (OSError, json.JSONDecodeError):
        pass
    return run_timestamp, campaign_id


class DropboxUploaderAgent(BaseAgent):
    """Final-step agent that snapshots ``outputs/`` to Dropbox.

    Opt-in via ``DROPBOX_UPLOAD_ENABLED=true``. Never raises — failures
    surface in the emitted event and the manifest, never as exceptions
    that could abort the agent graph."""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        if not _env_truthy("DROPBOX_UPLOAD_ENABLED"):
            text = (
                "Dropbox upload skipped (DROPBOX_UPLOAD_ENABLED is not set; "
                "set it to 'true' in .env or pass --upload-dropbox to enable)."
            )
            logger.info(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                actions=EventActions(state_delta={
                    "dropbox_upload_meta": {"enabled": False, "reason": "disabled"},
                }),
            )
            return

        report_path = state.get("report_path")
        if not report_path:
            text = "Dropbox upload skipped: no report_path in state (reporter didn't run)."
            logger.warning(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                actions=EventActions(state_delta={
                    "dropbox_upload_meta": {
                        "enabled": True, "reason": "no_report_path",
                    },
                }),
            )
            return

        run_timestamp, campaign_id = _extract_run_timestamp_and_campaign(report_path)
        if not campaign_id:
            text = (
                f"Dropbox upload skipped: campaign_id not found in {report_path}"
            )
            logger.warning(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                actions=EventActions(state_delta={
                    "dropbox_upload_meta": {
                        "enabled": True, "reason": "no_campaign_id",
                    },
                }),
            )
            return

        outputs_dir = Path(os.environ.get("OUTPUT_DIR", "outputs"))
        dropbox_root = os.environ.get("DROPBOX_ROOT_FOLDER", _DEFAULT_DROPBOX_ROOT)
        create_shared_links = _env_truthy("DROPBOX_CREATE_SHARED_LINKS")

        # Lazy import — keeps the SDK out of every non-upload run.
        from creative_pipeline.tools.dropbox_uploader import (
            upload_run_outputs,
            validate_dropbox_token,
        )

        # Pre-flight: two cheap API calls confirm both that the token is
        # valid AND that it has the ``files.content.write`` scope, before
        # we walk dozens of files and fire dozens of upload calls. On
        # failure, the manifest carries a single root-cause error instead
        # of N copies of the same per-file BadInputError/AuthError.
        ok, msg = validate_dropbox_token(
            verify_write=True, dropbox_root=dropbox_root,
        )
        if not ok:
            text = f"Dropbox upload skipped: token invalid → {msg}"
            logger.error(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                actions=EventActions(state_delta={
                    "dropbox_upload_meta": {
                        "enabled": True,
                        "reason": "invalid_token",
                        "error": msg,
                    },
                }),
            )
            return

        try:
            result = upload_run_outputs(
                outputs_dir=outputs_dir,
                campaign_id=campaign_id,
                run_timestamp=run_timestamp,
                dropbox_root=dropbox_root,
                create_shared_links=create_shared_links,
            )
        except RuntimeError as e:
            # Missing token, missing SDK, etc. — never raise into the
            # agent graph. Surface in the event + a structured meta entry.
            text = f"Dropbox upload failed: {e}"
            logger.error(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
                actions=EventActions(state_delta={
                    "dropbox_upload_meta": {
                        "enabled": True,
                        "reason": "config_error",
                        "error": str(e),
                    },
                }),
            )
            return

        # Write the local manifest mirroring the script-side shape.
        manifest_path = outputs_dir / f"dropbox_upload_{run_timestamp}.json"
        manifest = {
            "campaign_id": campaign_id,
            "run_timestamp": run_timestamp,
            "dropbox_run_folder": result["dropbox_run_folder"],
            "uploaded_count": result["uploaded_count"],
            "uploaded_files": result["uploaded_files"],
            "failures": result["failures"],
            "shared_links": result["shared_links"],
            "shared_link_failures": result["shared_link_failures"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2))

        text = (
            f"Dropbox upload: {result['uploaded_count']} files → "
            f"{result['dropbox_run_folder']} "
            f"(failures={len(result['failures'])}, "
            f"manifest={manifest_path})"
        )
        logger.info(text)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={
                "dropbox_upload_meta": {
                    "enabled": True,
                    "dropbox_run_folder": result["dropbox_run_folder"],
                    "uploaded_count": result["uploaded_count"],
                    "failures": len(result["failures"]),
                    "shared_links": len(result["shared_links"]),
                    "manifest_path": str(manifest_path),
                },
            }),
        )


dropbox_uploader_agent = DropboxUploaderAgent(name="DropboxUploaderAgent")
