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
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from google.adk.runners import InMemoryRunner
from google.genai import types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


_DEFAULT_DROPBOX_ROOT = "/TD-Creative-Pipeline-POC"
_REPORT_NAME_RE = re.compile(r"^report_\d{8}T\d{6}Z\.(json|md)$")


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
    parser.add_argument(
        "--clean-outputs",
        action="store_true",
        default=False,
        help="Delete everything under OUTPUT_DIR before the run "
             "(preserving outputs/.gitkeep). Equivalent to "
             "`rm -rf outputs/* && touch outputs/.gitkeep`. Default: off.",
    )
    parser.add_argument(
        "--keep-reports",
        type=int,
        default=None,
        metavar="N",
        help="After the run, prune older report_*.json/.md pairs and "
             "matching dropbox_upload_*.json manifests so only the "
             "newest N report timestamps remain. Default: keep everything.",
    )
    return parser.parse_args(argv)


def _clean_outputs_dir(outputs_dir: Path) -> int:
    """`--clean-outputs` implementation. Removes everything under
    ``outputs/`` (file or subdir), then re-touches ``.gitkeep`` so the
    directory stays tracked by git. Returns the number of top-level
    entries removed."""
    if not outputs_dir.exists():
        outputs_dir.mkdir(parents=True, exist_ok=True)
        (outputs_dir / ".gitkeep").touch()
        return 0
    removed = 0
    for entry in outputs_dir.iterdir():
        if entry.name == ".gitkeep":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1
    (outputs_dir / ".gitkeep").touch()
    return removed


def _prune_old_reports(outputs_dir: Path, keep: int) -> dict:
    """`--keep-reports N` implementation. Sorts the timestamped
    ``report_*.json`` files, keeps the newest N, deletes the rest along
    with their matching ``.md`` and ``dropbox_upload_*.json`` files.
    Never touches the convenience ``latest_report.{json,md}`` copies.

    Returns a dict with `kept` and `removed` lists (timestamps) for the
    final summary print."""
    if keep < 0:
        return {"kept": [], "removed": []}
    json_reports = sorted(
        (p for p in outputs_dir.glob("report_*.json") if _REPORT_NAME_RE.match(p.name)),
        key=lambda p: p.name,
    )
    if len(json_reports) <= keep:
        return {
            "kept": [p.stem.removeprefix("report_") for p in json_reports],
            "removed": [],
        }
    to_keep = set(json_reports[-keep:]) if keep > 0 else set()
    removed_ts: list[str] = []
    for p in json_reports:
        if p in to_keep:
            continue
        ts = p.stem.removeprefix("report_")
        # Pair: matching .md + matching dropbox_upload_<ts>.json
        for sibling in (
            p,
            outputs_dir / f"report_{ts}.md",
            outputs_dir / f"dropbox_upload_{ts}.json",
        ):
            try:
                sibling.unlink(missing_ok=True)
            except OSError:
                pass
        removed_ts.append(ts)
    return {
        "kept": [p.stem.removeprefix("report_") for p in to_keep],
        "removed": removed_ts,
    }


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


def _print_compact_summary(
    args: argparse.Namespace,
    final_state: dict,
    pruned: dict | None,
) -> None:
    """One-screen summary at the end of the run so the operator doesn't
    have to scroll through a multi-thousand-line log to see whether
    anything failed. Reads paths from final state, opens the Markdown
    report for status flags."""
    json_path = final_state.get("report_path") or "<missing>"
    md_path = final_state.get("markdown_report_path") or "<missing>"
    db_meta = final_state.get("dropbox_upload_meta") or {}

    # Pull overall statuses from the JSON report (single source of truth).
    brand = legal = qc = "unknown"
    products_summary: list[str] = []
    if final_state.get("report_path"):
        try:
            report = json.loads(Path(final_state["report_path"]).read_text())
            ranks = {"pass": 0, "n/a": 0, None: 0, "warn": 1, "fail": 2}
            def _worst(values):
                vals = list(values)
                return max(vals, key=lambda v: ranks.get(v, 0)) if vals else "n/a"
            brand = _worst(p.get("brand_check_summary") for p in report.get("products", []))
            legal = _worst(p.get("legal_check_summary") for p in report.get("products", []))
            qc = _worst(p.get("qc_check_summary") for p in report.get("products", []))
            for p in report.get("products", []):
                products_summary.append(
                    f"  - {p.get('product_id', '?')}: {len(p.get('outputs') or [])} "
                    f"outputs, QC {p.get('qc_check_summary', 'n/a')}"
                )
        except (OSError, json.JSONDecodeError):
            pass

    print()
    print("Run complete.")
    print(f"Report JSON:     {json_path}")
    print(f"Report Markdown: {md_path}")
    if Path("outputs/latest_report.md").exists():
        print(f"Latest copies:   outputs/latest_report.json, outputs/latest_report.md")
    if db_meta.get("enabled"):
        if "dropbox_run_folder" in db_meta:
            print(
                f"Dropbox:         uploaded {db_meta.get('uploaded_count', 0)} files "
                f"to {db_meta['dropbox_run_folder']} "
                f"(failures={db_meta.get('failures', 0)})"
            )
        else:
            print(
                f"Dropbox:         skipped ({db_meta.get('reason', 'unknown')})"
            )
    else:
        print("Dropbox:         not enabled (set DROPBOX_UPLOAD_ENABLED=true to upload)")
    if pruned and pruned.get("removed"):
        print(
            f"Reports pruned:  kept {len(pruned['kept'])}, "
            f"removed {len(pruned['removed'])} older report(s)"
        )
    print()
    print("Overall:")
    print(f"  Brand: {brand}")
    print(f"  Legal: {legal}")
    print(f"  QC:    {qc}")
    if products_summary:
        print()
        print("Products:")
        for line in products_summary:
            print(line)
    print()


async def main() -> int:
    args = _parse_args()
    _apply_dropbox_env_from_args(args)

    outputs_dir = Path(os.environ.get("OUTPUT_DIR", "outputs"))
    if args.clean_outputs:
        removed_count = _clean_outputs_dir(outputs_dir)
        print(f"CLEAN_OUTPUTS: removed {removed_count} entries from {outputs_dir}/ "
              f"(preserved .gitkeep)")

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

    # Optional retention pruning — runs AFTER the report is written so
    # the freshly-written report is always among the "newest N" kept.
    pruned: dict | None = None
    if args.keep_reports is not None:
        pruned = _prune_old_reports(outputs_dir, args.keep_reports)

    # Compact summary for at-a-glance scanning (replaces the older
    # REPORT: / DROPBOX_UPLOAD: lines; full info lives in the Markdown
    # report at outputs/report_<ts>.md).
    _print_compact_summary(args, dict(final.state), pruned)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
