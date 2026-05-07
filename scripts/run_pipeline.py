"""One-shot driver to run the full root_agent pipeline non-interactively.

Used by reviewers / CI to trigger an end-to-end render without spinning up
`adk web`. Reads the standard env (PROVIDER, MODEL, IMAGE_PROVIDER, etc.)
and prints the report path on completion.

Optional Dropbox upload is performed by ``DropboxUploaderAgent`` as the
final step in the agent graph (so ``adk web`` runs upload too when env
is configured). The CLI flags below just *translate* into the env vars
the agent reads — no separate upload code path lives here:
    python scripts/run_pipeline.py --upload-dropbox
    python scripts/run_pipeline.py --upload-dropbox --dropbox-shared-links
    python scripts/run_pipeline.py --upload-dropbox --dropbox-root /Custom/Folder
The Dropbox SDK is an optional extra — `pip install -e .[upload]`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from google.adk.runners import InMemoryRunner
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_DEFAULT_DROPBOX_ROOT = "/TD-Creative-Pipeline-POC"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the creative pipeline end-to-end. Optional Dropbox "
                    "upload is handled by the in-graph DropboxUploaderAgent — "
                    "the flags below just translate into env vars the agent reads.",
    )
    parser.add_argument(
        "--upload-dropbox",
        action="store_true",
        default=_env_truthy("DROPBOX_UPLOAD_ENABLED"),
        help="Sets DROPBOX_UPLOAD_ENABLED=true so the in-graph "
             "DropboxUploaderAgent uploads outputs/ after the pipeline "
             "finishes. Default: off.",
    )
    parser.add_argument(
        "--dropbox-root",
        default=os.environ.get("DROPBOX_ROOT_FOLDER", _DEFAULT_DROPBOX_ROOT),
        help=f"Sets DROPBOX_ROOT_FOLDER (default: {_DEFAULT_DROPBOX_ROOT}).",
    )
    parser.add_argument(
        "--dropbox-shared-links",
        action="store_true",
        default=_env_truthy("DROPBOX_CREATE_SHARED_LINKS"),
        help="Sets DROPBOX_CREATE_SHARED_LINKS=true (best-effort public "
             "links for report/gallery files). Default: off.",
    )
    return parser.parse_args(argv)


def _apply_dropbox_env_from_args(args: argparse.Namespace) -> None:
    """Translate CLI flags into env vars before the agent graph runs.
    The DropboxUploaderAgent reads env directly so this lets `adk web`
    and `scripts/run_pipeline.py` share one upload code path."""
    if args.upload_dropbox:
        os.environ["DROPBOX_UPLOAD_ENABLED"] = "true"
    if args.dropbox_root:
        os.environ["DROPBOX_ROOT_FOLDER"] = args.dropbox_root
    if args.dropbox_shared_links:
        os.environ["DROPBOX_CREATE_SHARED_LINKS"] = "true"


def _dropbox_preflight_or_warn() -> None:
    """When DROPBOX_UPLOAD_ENABLED is on, do two cheap API calls to confirm
    the token actually works AND has the ``files.content.write`` scope
    before the pipeline burns minutes on image generation. On failure:
    warn loudly and CONTINUE — local outputs are still valuable, and the
    agent's per-file error capture will record the problem in the upload
    manifest. On success: print a one-line `DROPBOX_PREFLIGHT: ...`
    confirmation so operators see green at second 0."""
    if not _env_truthy("DROPBOX_UPLOAD_ENABLED"):
        return
    from creative_pipeline.tools.dropbox_uploader import validate_dropbox_token

    dropbox_root = os.environ.get("DROPBOX_ROOT_FOLDER", _DEFAULT_DROPBOX_ROOT)
    ok, msg = validate_dropbox_token(verify_write=True, dropbox_root=dropbox_root)
    if ok:
        print(f"DROPBOX_PREFLIGHT: {msg}")
        return
    print("", file=sys.stderr)
    print(f"⚠️  DROPBOX_PREFLIGHT WARNING: {msg}", file=sys.stderr)
    print("    Regenerate / fix scopes at: https://www.dropbox.com/developers/apps", file=sys.stderr)
    print("    Make sure these scopes are enabled on the Permissions tab:", file=sys.stderr)
    print("        - account_info.read       (for token validation)", file=sys.stderr)
    print("        - files.content.write     (for upload — most-missed scope)", file=sys.stderr)
    print("        - sharing.write           (only if --dropbox-shared-links)", file=sys.stderr)
    print("    Then regenerate the token (Settings → Generated access token).", file=sys.stderr)
    print("    The pipeline will still run — local outputs/ remain unaffected.", file=sys.stderr)
    print("    Dropbox upload will fail per-file (recorded in manifest).", file=sys.stderr)
    print("", file=sys.stderr)


async def main() -> int:
    args = _parse_args()
    _apply_dropbox_env_from_args(args)
    _dropbox_preflight_or_warn()

    # Import the agent AFTER env vars are applied so any future env-driven
    # initialization sees the right values.
    from creative_pipeline.agent import root_agent

    runner = InMemoryRunner(agent=root_agent, app_name="creative_pipeline_oneshot")
    user_id = "cli"
    session = await runner.session_service.create_session(
        app_name="creative_pipeline_oneshot", user_id=user_id
    )
    msg = types.Content(role="user", parts=[types.Part(text="run")])
    async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    logger.info("[%s] %s", event.author or "agent", part.text)
    final = await runner.session_service.get_session(
        app_name="creative_pipeline_oneshot", user_id=user_id, session_id=session.id
    )
    report_path = final.state.get("report_path")
    if not report_path:
        print("ERROR: pipeline finished without a report_path", file=sys.stderr)
        return 1
    print(f"REPORT: {report_path}")

    # Surface the Dropbox upload result for at-a-glance scanning. The
    # agent has already written the manifest and emitted its summary
    # event; this line just lifts the structured meta into stdout.
    db_meta = final.state.get("dropbox_upload_meta") or {}
    if db_meta.get("enabled"):
        if "dropbox_run_folder" in db_meta:
            print(
                f"DROPBOX_UPLOAD: enabled=true folder={db_meta['dropbox_run_folder']} "
                f"files={db_meta.get('uploaded_count', 0)} "
                f"failures={db_meta.get('failures', 0)}"
            )
            if db_meta.get("manifest_path"):
                print(f"DROPBOX_MANIFEST: {db_meta['manifest_path']}")
        else:
            # Enabled but couldn't run — agent recorded a reason
            # (no_report_path, no_campaign_id, config_error, …).
            print(
                f"DROPBOX_UPLOAD: enabled=true but skipped "
                f"({db_meta.get('reason', 'unknown')})",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
