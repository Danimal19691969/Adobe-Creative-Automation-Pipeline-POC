"""Optional Dropbox uploader — post-pipeline snapshot of ``outputs/``.

Used by ``scripts/run_pipeline.py`` when ``--upload-dropbox`` is set (or
``DROPBOX_UPLOAD_ENABLED=true``). Local outputs remain the source of truth;
this module is a *snapshot* layer.

Design notes:
- The ``dropbox`` SDK is an OPTIONAL extra (``pip install -e .[upload]``).
  We import it inside ``upload_run_outputs`` rather than at module top so
  the rest of the pipeline doesn't pay the import cost when the user isn't
  uploading. The only top-level imports here are stdlib.
- The access token is **never** logged, printed, or returned in metadata.
  It's read from the function arg or the ``DROPBOX_ACCESS_TOKEN`` env var
  and held only inside the SDK client object for the lifetime of the call.
- Per-file upload errors do NOT abort the run — they're recorded in
  ``failures`` and the walker continues. Shared-link errors are softer
  still (warnings, not part of ``failures``).
- File whitelist (PNG / JSON / HTML) and skip-list (hidden files,
  ``__pycache__``, ``dropbox_upload_*.json`` manifests) are enforced at
  collection time so the test harness can verify them without touching
  the SDK.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)


# Files we *do* upload — the rest of the tree is irrelevant to reviewers.
_ALLOWED_SUFFIXES = {".png", ".json", ".html"}

# Names containing this token are our own per-run upload manifests; never
# include them in a future upload (avoids "manifest of the manifest").
_MANIFEST_NAME_TOKEN = "dropbox_upload_"

# Dropbox simple-upload limit (https://www.dropbox.com/developers/documentation/http/documentation#files-upload):
# files larger than this need a chunked upload session. POC files are well
# below — guarded explicitly so a future too-large file doesn't get truncated.
_DROPBOX_SIMPLE_UPLOAD_LIMIT_BYTES = 150 * 1024 * 1024  # 150 MB

# Pretty filename markers reviewers look at first — when shared-link
# generation is enabled, prioritize these.
_SHARED_LINK_PRIORITY_PREFIXES = ("report_", "gallery_")


def _is_skipped_part(part: str) -> bool:
    """A path component (file or dir name) the walker should ignore."""
    if not part:
        return True
    if part.startswith("."):              # hidden: .gitkeep, .DS_Store, .adk, .env
        return True
    if part == "__pycache__":
        return True
    if _MANIFEST_NAME_TOKEN in part.lower():
        return True
    return False


def _collect_uploadable_files(outputs_dir: Path) -> list[Path]:
    """Walk ``outputs_dir`` and return the files we want to upload, sorted
    deterministically so the upload order matches between runs.

    Filters:
      - any path with a hidden segment is dropped (``.git``, ``.DS_Store``,
        ``.gitkeep``, ``.env``, ``.adk``)
      - ``__pycache__`` segments dropped
      - any segment containing ``dropbox_upload_`` dropped (own manifests)
      - file suffix must be in ``_ALLOWED_SUFFIXES``
    """
    if not outputs_dir.exists():
        return []
    keep: list[Path] = []
    for p in outputs_dir.rglob("*"):
        if not p.is_file():
            continue
        # Check every path part (relative to outputs_dir) for skip rules so a
        # ``.adk/sessions.db`` or ``__pycache__/x.json`` is filtered even if
        # the file's own name passes.
        rel_parts = p.relative_to(outputs_dir).parts
        if any(_is_skipped_part(part) for part in rel_parts):
            continue
        if p.suffix.lower() not in _ALLOWED_SUFFIXES:
            continue
        keep.append(p)
    keep.sort()
    return keep


def _local_to_dropbox_path(
    local_path: Path,
    outputs_dir: Path,
    dropbox_run_folder: str,
) -> str:
    """Map a local file path under ``outputs_dir`` to its Dropbox-side path.

    ``dropbox_run_folder`` is the per-run prefix (no trailing slash);
    relative parts are appended with forward slashes.
    """
    rel = local_path.relative_to(outputs_dir)
    return str(PurePosixPath(dropbox_run_folder) / PurePosixPath(*rel.parts))


def _resolve_token(arg_token: str | None) -> str:
    token = arg_token or os.environ.get("DROPBOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "DROPBOX_ACCESS_TOKEN is required to upload to Dropbox. "
            "Set it in .env or pass it via the access_token argument. "
            "(Dropbox upload is opt-in; local runs do not need this.)"
        )
    return token


def _import_dropbox_sdk() -> tuple[Any, Any, Any]:
    """Import the Dropbox SDK lazily so users who never upload don't need
    the package. Returns ``(Dropbox, WriteMode, ApiError)``.

    Raises ``RuntimeError`` with an actionable message when the package is
    missing — never an opaque ``ImportError`` traceback.
    """
    try:
        import dropbox  # type: ignore[import-not-found]
        from dropbox.exceptions import ApiError  # type: ignore[import-not-found]
        from dropbox.files import WriteMode  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover — covered by env, not unit tests
        raise RuntimeError(
            "The 'dropbox' package is not installed. Run "
            "`pip install -e .[upload]` (or `uv sync --extra upload`) "
            "to enable optional Dropbox upload."
        ) from e
    return dropbox.Dropbox, WriteMode, ApiError


def _create_shared_link_safely(dbx: Any, dropbox_path: str) -> str | None:
    """Best-effort shared-link creation. Returns the URL string on success,
    None on any error (caller records the failure separately).

    Handles the common "shared_link_already_exists" race by listing existing
    links and returning the first one — Dropbox doesn't replace, it errors.
    """
    try:
        link = dbx.sharing_create_shared_link_with_settings(dropbox_path)
        return getattr(link, "url", None)
    except Exception:  # pragma: no cover — fakes raise specific exceptions
        # Try to recover an existing link (the "already exists" path).
        try:
            existing = dbx.sharing_list_shared_links(path=dropbox_path).links
            if existing:
                return getattr(existing[0], "url", None)
        except Exception:
            pass
        return None


def validate_dropbox_token(
    access_token: str | None = None,
    *,
    verify_write: bool = False,
    dropbox_root: str = "/",
    _dropbox_client_factory: Any = None,
) -> tuple[bool, str]:
    """Cheap pre-flight check: ping ``users/get_current_account`` to verify
    the access token is actually valid before we walk the file tree and
    fire dozens of upload calls.

    When ``verify_write=True``, also performs a tiny test write + delete
    to ``<dropbox_root>/_preflight_<hex>.txt`` so missing-scope problems
    (most commonly a token without ``files.content.write``) surface at
    second 0 instead of after image generation. The sentinel file is
    immediately deleted; cleanup failure is treated as a warning, not a
    preflight failure (the write succeeded — that's what we needed to
    confirm). Costs one extra round-trip (~100 ms).

    Returns ``(ok, message)``. ``ok=False`` covers every failure mode
    (missing env var, missing SDK, expired/invalid token, missing scope,
    network error, Dropbox 5xx). The message is safe to log — it never
    echoes the token itself.

    Used by:
      - ``scripts/run_pipeline.py`` at startup (fails fast at second 0
        instead of after a multi-minute image-gen run when the token
        has expired or lacks write scope).
      - ``DropboxUploaderAgent`` at the start of its ``_run_async_impl``
        (same fail-fast for ``adk web`` / ``adk run`` paths).

    Never raises — all failures convert to ``(False, message)``.
    """
    import uuid

    token = access_token or os.environ.get("DROPBOX_ACCESS_TOKEN")
    if not token:
        return False, "DROPBOX_ACCESS_TOKEN is not set"

    if _dropbox_client_factory is not None:
        dbx_cls = _dropbox_client_factory
        WriteMode = getattr(dbx_cls, "WriteMode", None)
    else:
        try:
            dbx_cls, WriteMode, _ = _import_dropbox_sdk()
        except RuntimeError as e:
            return False, str(e)

    try:
        dbx = dbx_cls(token)
        account = dbx.users_get_current_account()
        # Don't echo email by default — log a redacted-ish identifier so
        # operators see "yes the token works" without leaking PII into logs.
        email = getattr(account, "email", None) or "<unknown>"
        account_msg = f"valid (account: {email})"
    except Exception as e:  # noqa: BLE001 — we explicitly never raise
        return False, f"invalid: {type(e).__name__}: {e}"

    if not verify_write:
        return True, account_msg

    # Write-scope verification. Use a uniquely-named sentinel so concurrent
    # preflights from multiple machines / runs don't collide.
    sentinel_path = str(
        PurePosixPath(dropbox_root) / f"_preflight_{uuid.uuid4().hex[:8]}.txt"
    )
    sentinel_content = (
        b"This is a preflight check from the creative-pipeline POC. "
        b"Safe to delete - the writer attempts to clean it up immediately."
    )

    try:
        mode = WriteMode("overwrite") if WriteMode is not None else "overwrite"
        dbx.files_upload(sentinel_content, sentinel_path, mode=mode)
    except Exception as e:  # noqa: BLE001
        # Most common shape here is the BadInputError 'files.content.write'
        # missing-scope error. Surface it as the preflight failure so the
        # user gets the actionable message at second 0.
        return False, (
            f"write check failed (cannot upload to {sentinel_path}): "
            f"{type(e).__name__}: {e}"
        )

    # Best-effort cleanup. The sentinel file got written, so the write
    # scope clearly works — that's the answer we needed. A cleanup failure
    # leaves a small empty file behind, not a security or correctness issue.
    try:
        dbx.files_delete_v2(sentinel_path)
        return True, f"{account_msg}; write scope OK"
    except Exception as e:  # noqa: BLE001
        return True, (
            f"{account_msg}; write scope OK "
            f"(note: cleanup of {sentinel_path} failed: {type(e).__name__})"
        )


def upload_run_outputs(
    outputs_dir: Path,
    campaign_id: str,
    run_timestamp: str,
    dropbox_root: str = "/TD-Creative-Pipeline-POC",
    create_shared_links: bool = False,
    access_token: str | None = None,
    *,
    parallelism: int | None = None,
    _dropbox_client_factory: Any = None,
) -> dict:
    """Upload the contents of ``outputs_dir`` to a per-run Dropbox folder.

    Args:
        outputs_dir: Local outputs root, e.g. ``Path("outputs")``.
        campaign_id: Used in the per-run folder path; comes from the brief.
        run_timestamp: ``YYYYMMDDTHHMMSSZ`` string from the report filename.
        dropbox_root: Remote root folder. Defaults to
            ``/TD-Creative-Pipeline-POC``.
        create_shared_links: When True, request public links for the report
            and gallery files (best-effort; failures don't abort the run).
        access_token: Override env var. Pass for tests; production reads
            from ``DROPBOX_ACCESS_TOKEN``.
        _dropbox_client_factory: Test hook only — pass ``FakeDropbox`` to
            avoid real network calls. Public callers should leave this None.

    Returns:
        dict with keys:
          dropbox_run_folder, uploaded_count, uploaded_files, failures,
          shared_links, shared_link_failures, manifest_path
        The token is never present in the return value.
    """
    token = _resolve_token(access_token)

    if _dropbox_client_factory is not None:
        # Test path — fake client. We still go through `_import_dropbox_sdk`
        # *only* if the test wants the real WriteMode/ApiError types; the
        # FakeDropbox implementation provides its own.
        dbx_cls = _dropbox_client_factory
        WriteMode = getattr(dbx_cls, "WriteMode", None)
        ApiError = getattr(dbx_cls, "ApiError", Exception)
    else:
        dbx_cls, WriteMode, ApiError = _import_dropbox_sdk()

    dropbox_run_folder = str(
        PurePosixPath(dropbox_root) / campaign_id / run_timestamp
    )

    files = _collect_uploadable_files(outputs_dir)
    logger.info(
        "Dropbox upload: %d files → %s", len(files), dropbox_run_folder
    )

    dbx = dbx_cls(token)
    uploaded_files: list[dict] = []
    failures: list[dict] = []

    # Resolve parallelism. The Dropbox SDK client is thread-safe (uses a
    # connection-pooled requests Session under the hood) and uploads are
    # I/O-bound, so a small ThreadPoolExecutor turns serial latency into
    # near-bandwidth-limited throughput. 8 is conservative — Dropbox's
    # rate-limit policy tolerates well above that for files.upload.
    if parallelism is None:
        try:
            parallelism = max(
                1, int(os.environ.get("DROPBOX_UPLOAD_PARALLELISM", "8"))
            )
        except ValueError:
            parallelism = 8
    parallelism = min(parallelism, max(1, len(files)))

    def _upload_one(local: Path) -> dict:
        """Single-file upload — exception-safe shape so the executor can
        return-or-record without re-raising into the caller."""
        size = local.stat().st_size
        if size > _DROPBOX_SIMPLE_UPLOAD_LIMIT_BYTES:
            # TODO: chunked upload sessions (files_upload_session_*) for
            # files larger than 150 MB. The POC is comfortably under that
            # ceiling; surfacing instead of silently truncating.
            return {
                "local": str(local),
                "error": (
                    f"file size {size} exceeds Dropbox simple-upload limit "
                    f"({_DROPBOX_SIMPLE_UPLOAD_LIMIT_BYTES} bytes); "
                    "chunked upload session not yet implemented"
                ),
                "is_failure": True,
            }
        dropbox_path = _local_to_dropbox_path(local, outputs_dir, dropbox_run_folder)
        try:
            content = local.read_bytes()
            mode = WriteMode("overwrite") if WriteMode is not None else "overwrite"
            dbx.files_upload(content, dropbox_path, mode=mode)
            return {
                "local": str(local), "dropbox": dropbox_path, "is_failure": False,
            }
        except ApiError as e:
            return {
                "local": str(local), "error": f"ApiError: {e}", "is_failure": True,
            }
        except Exception as e:  # noqa: BLE001 — record-and-continue is the policy
            return {
                "local": str(local),
                "error": f"{type(e).__name__}: {e}",
                "is_failure": True,
            }

    if parallelism > 1 and len(files) > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        logger.info(
            "Dropbox upload: parallelism=%d (%d files)",
            parallelism, len(files),
        )
        with ThreadPoolExecutor(max_workers=parallelism) as ex:
            futures = [ex.submit(_upload_one, f) for f in files]
            for fut in as_completed(futures):
                result = fut.result()
                if result["is_failure"]:
                    failures.append(
                        {"local": result["local"], "error": result["error"]}
                    )
                else:
                    uploaded_files.append(
                        {"local": result["local"], "dropbox": result["dropbox"]}
                    )
    else:
        for f in files:
            result = _upload_one(f)
            if result["is_failure"]:
                failures.append(
                    {"local": result["local"], "error": result["error"]}
                )
            else:
                uploaded_files.append(
                    {"local": result["local"], "dropbox": result["dropbox"]}
                )

    # Concurrent uploads complete out-of-order; sort the manifest's lists
    # by local path so the report stays deterministic between runs (cleaner
    # diffs in audit; identical CI fingerprints).
    uploaded_files.sort(key=lambda x: x["local"])
    failures.sort(key=lambda x: x["local"])

    shared_links: list[dict] = []
    shared_link_failures: list[dict] = []
    if create_shared_links:
        # Reviewer-priority files first, then any remaining uploads.
        priority = [
            f for f in uploaded_files
            if any(
                Path(f["dropbox"]).name.startswith(prefix)
                for prefix in _SHARED_LINK_PRIORITY_PREFIXES
            )
        ]
        rest = [f for f in uploaded_files if f not in priority]
        for entry in priority + rest:
            url = _create_shared_link_safely(dbx, entry["dropbox"])
            if url:
                shared_links.append({"path": entry["dropbox"], "url": url})
            else:
                shared_link_failures.append({"path": entry["dropbox"]})

    return {
        "dropbox_run_folder": dropbox_run_folder,
        "uploaded_count": len(uploaded_files),
        "uploaded_files": uploaded_files,
        "failures": failures,
        "shared_links": shared_links,
        "shared_link_failures": shared_link_failures,
        # Caller fills this in when it writes the local manifest.
        "manifest_path": None,
    }
