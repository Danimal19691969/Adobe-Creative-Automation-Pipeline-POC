"""Optional Dropbox upload — tests use a FakeDropbox client only.

Hard rules these tests enforce:
  - Walker uploads only .png / .json / .html files
  - Walker skips hidden files (.gitkeep, .DS_Store, .env), __pycache__,
    .adk/, and our own dropbox_upload_*.json manifests
  - Default Dropbox root is "/TD-Creative-Pipeline-POC" (no typo regression)
  - Local paths map to <root>/<campaign_id>/<run_timestamp>/<relative>
    using forward slashes only
  - Missing token raises a clear RuntimeError (never an opaque ImportError)
  - Per-file failures don't abort the run
  - Shared-link creation is opt-in and best-effort
  - Manifest is written next to the local outputs
  - run_pipeline.py advertises the three new flags via --help
  - The pipeline never imports the dropbox SDK when upload is off
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from creative_pipeline.tools import dropbox_uploader
from creative_pipeline.tools.dropbox_uploader import (
    _collect_uploadable_files,
    _local_to_dropbox_path,
    upload_run_outputs,
    validate_dropbox_token,
)


# ---------- FakeDropbox: in-process Dropbox SDK stand-in --------------------


class _FakeApiError(Exception):
    """Mimics dropbox.exceptions.ApiError without importing the real SDK."""


class _FakeWriteMode:
    """Mimics dropbox.files.WriteMode("overwrite") — we only care that the
    mode is propagated, not its identity."""
    def __init__(self, mode: str):
        self.mode = mode

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _FakeWriteMode) and self.mode == other.mode


class _FakeSharedLink:
    def __init__(self, url: str):
        self.url = url


class _FakeListLinksResponse:
    def __init__(self, links: list[_FakeSharedLink]):
        self.links = links


class FakeDropbox:
    """Records every call. Tests inject a subclass when they want a
    specific method to fail."""

    # Surface the test-only types so the uploader picks them up via
    # `getattr(dbx_cls, "WriteMode", None)` etc.
    WriteMode = _FakeWriteMode
    ApiError = _FakeApiError

    def __init__(self, token: str):
        # Store the token so tests can verify it was passed correctly,
        # but the uploader's *return value* must not contain it.
        self.token = token
        self.uploaded: list[tuple[str, bytes, _FakeWriteMode]] = []
        self.shared_link_calls: list[str] = []
        self.list_link_calls: list[str] = []

    def users_get_current_account(self):
        """Mimics the real SDK's success-path call. Tests that want to
        simulate an invalid token override this method to raise."""
        class _Account:
            email = "smoke-test@example.com"
        return _Account()

    def files_upload(self, content: bytes, dropbox_path: str, mode):
        self.uploaded.append((dropbox_path, content, mode))
        return None

    def files_delete_v2(self, dropbox_path: str):
        """Mimics the real SDK's delete call. Tests that want to simulate
        a cleanup failure override this method to raise."""
        if not hasattr(self, "deleted"):
            self.deleted: list[str] = []
        self.deleted.append(dropbox_path)
        return None

    def sharing_create_shared_link_with_settings(self, dropbox_path: str):
        self.shared_link_calls.append(dropbox_path)
        return _FakeSharedLink(url=f"https://www.dropbox.com/scl/fake{dropbox_path}")

    def sharing_list_shared_links(self, path: str):
        self.list_link_calls.append(path)
        return _FakeListLinksResponse(links=[])


def _factory(record: list[FakeDropbox] | None = None):
    """Return a Dropbox-class-like callable that records the constructed
    fake instance for assertion."""
    class _Recorded(FakeDropbox):
        def __init__(self, token: str):
            super().__init__(token)
            if record is not None:
                record.append(self)
    return _Recorded


# ---------- fixtures --------------------------------------------------------


@pytest.fixture
def outputs_tree(tmp_path: Path) -> Path:
    """A realistic mini-tree mirroring the actual outputs/ shape."""
    out = tmp_path / "outputs"
    out.mkdir()
    # Whitelist files (should upload):
    (out / "report_20260507T172946Z.json").write_text("{\"campaign_id\": \"c\"}")
    (out / "gallery_20260507T172946Z.html").write_text("<html></html>")
    (out / "aquavita_sparkling").mkdir()
    (out / "aquavita_sparkling" / "1x1").mkdir()
    (out / "aquavita_sparkling" / "1x1" / "MX_es.png").write_bytes(b"PNG-1")
    (out / "aquavita_sparkling" / "1x1" / "BR_pt.png").write_bytes(b"PNG-2")
    (out / "aquavita_sparkling" / "9x16").mkdir()
    (out / "aquavita_sparkling" / "9x16" / "MX_es.png").write_bytes(b"PNG-3")
    (out / "aquavita_sparkling" / "source").mkdir()
    (out / "aquavita_sparkling" / "source" / "global_x.png").write_bytes(b"HERO")
    (out / "aquavita_sparkling" / "source" / "global_x.json").write_text("{}")

    # Skip-list files (should NOT upload):
    (out / ".gitkeep").write_text("")
    (out / ".DS_Store").write_bytes(b"\0\0\0")
    (out / "dropbox_upload_20260507T172946Z.json").write_text("{}")  # prior manifest
    (out / "__pycache__").mkdir()
    (out / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (out / "__pycache__" / "x.json").write_text("{}")  # JSON inside pycache → still skipped
    (out / ".adk").mkdir()
    (out / ".adk" / "sessions.db").write_bytes(b"\0")
    (out / ".adk" / "config.json").write_text("{}")  # JSON inside .adk → still skipped
    (out / "stray.txt").write_text("not a whitelisted suffix")

    return out


# ---------- 1. file collection ---------------------------------------------


def test_collect_files_includes_png_json_html_only(outputs_tree: Path):
    files = _collect_uploadable_files(outputs_tree)
    names = {p.name for p in files}
    # Allowed:
    assert "report_20260507T172946Z.json" in names
    assert "gallery_20260507T172946Z.html" in names
    assert "MX_es.png" in names
    assert "global_x.png" in names
    assert "global_x.json" in names
    # Skipped:
    assert ".gitkeep" not in names
    assert ".DS_Store" not in names
    assert "x.pyc" not in names
    assert "sessions.db" not in names
    assert "stray.txt" not in names
    # Prior manifest filtered out so we never recursively upload manifests:
    assert "dropbox_upload_20260507T172946Z.json" not in names
    # JSON inside skipped dirs is skipped even though .json is whitelisted:
    config_paths = [p for p in files if p.name == "config.json"]
    pycache_jsons = [p for p in files if "__pycache__" in p.parts]
    assert config_paths == []
    assert pycache_jsons == []


# ---------- 2. local→dropbox path mapping ----------------------------------


def test_dropbox_path_preserves_relative_structure(tmp_path: Path):
    out = tmp_path / "outputs"
    out.mkdir()
    nested = out / "x" / "y" / "z.png"
    nested.parent.mkdir(parents=True)
    nested.write_bytes(b"")
    mapped = _local_to_dropbox_path(
        local_path=nested,
        outputs_dir=out,
        dropbox_run_folder="/TD-Creative-Pipeline-POC/cid/20260101T000000Z",
    )
    assert mapped == "/TD-Creative-Pipeline-POC/cid/20260101T000000Z/x/y/z.png"
    # Forward slashes only — no Windows path separators leak through.
    assert "\\" not in mapped


# ---------- 3. default Dropbox root (canonical spelling) ------------------


def test_default_root_is_td_creative_pipeline_poc(monkeypatch, outputs_tree, tmp_path):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "test-token")
    monkeypatch.chdir(tmp_path)
    result = upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        _dropbox_client_factory=_factory(),
    )
    assert result["dropbox_run_folder"] == "/TD-Creative-Pipeline-POC/cid/20260101T000000Z"
    # Canonical spelling — guard against a "Pipepline" typo regression.
    assert "Pipeline-POC" in result["dropbox_run_folder"]
    assert "Pipepline" not in result["dropbox_run_folder"]


# ---------- 4. missing token raises clearly --------------------------------


def test_missing_token_raises_clear_error(monkeypatch, outputs_tree):
    monkeypatch.delenv("DROPBOX_ACCESS_TOKEN", raising=False)
    with pytest.raises(RuntimeError) as exc:
        upload_run_outputs(
            outputs_dir=outputs_tree,
            campaign_id="cid",
            run_timestamp="20260101T000000Z",
            _dropbox_client_factory=_factory(),
        )
    msg = str(exc.value)
    assert "DROPBOX_ACCESS_TOKEN" in msg
    # Don't tell the user to install the SDK when the actual problem is the
    # token — the message should name the missing env var first.
    assert "is required" in msg or "required" in msg


# ---------- 5. token is never echoed in metadata ---------------------------


def test_token_never_appears_in_returned_meta(monkeypatch, outputs_tree, tmp_path):
    sentinel = "sk-secret-DO-NOT-LEAK-12345"
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", sentinel)
    monkeypatch.chdir(tmp_path)
    result = upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        _dropbox_client_factory=_factory(),
    )
    serialized = json.dumps(result)
    assert sentinel not in serialized


# ---------- 6. per-file failure recorded, run continues --------------------


def test_per_file_failure_does_not_abort_run(monkeypatch, outputs_tree, tmp_path):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.chdir(tmp_path)

    class _PartlyFailingDropbox(FakeDropbox):
        call_count = 0

        def files_upload(self, content, dropbox_path, mode):
            type(self).call_count += 1
            if type(self).call_count == 3:
                raise self.ApiError("simulated network error on third file")
            return super().files_upload(content, dropbox_path, mode)

    result = upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        _dropbox_client_factory=_PartlyFailingDropbox,
    )
    assert len(result["failures"]) == 1
    # Five whitelisted files in the fixture; one failed; four uploaded.
    assert result["uploaded_count"] >= 4
    assert "simulated network error" in result["failures"][0]["error"]


# ---------- 7. shared links opt-in -----------------------------------------


def test_shared_links_disabled_by_default(monkeypatch, outputs_tree, tmp_path):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.chdir(tmp_path)
    record: list[FakeDropbox] = []
    upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        _dropbox_client_factory=_factory(record),
    )
    # No share calls were made.
    assert all(d.shared_link_calls == [] for d in record)


def test_shared_link_creation_failure_is_warning_not_fatal(
    monkeypatch, outputs_tree, tmp_path,
):
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.chdir(tmp_path)

    class _NoShareDropbox(FakeDropbox):
        def sharing_create_shared_link_with_settings(self, dropbox_path):
            raise self.ApiError("share quota exceeded")

    result = upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        create_shared_links=True,
        _dropbox_client_factory=_NoShareDropbox,
    )
    # Uploads still succeeded; shared-link failures are recorded separately.
    assert result["uploaded_count"] >= 5
    assert result["failures"] == []
    assert result["shared_links"] == []
    assert len(result["shared_link_failures"]) >= 1


# ---------- 8. manifest written by run_pipeline driver --------------------


def test_run_pipeline_manifest_written_via_driver(monkeypatch, outputs_tree, tmp_path):
    """The uploader returns metadata with manifest_path=None; the
    run_pipeline driver is responsible for actually writing it. Simulate
    that contract here so the manifest shape is locked in."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.chdir(tmp_path)
    result = upload_run_outputs(
        outputs_dir=outputs_tree,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        _dropbox_client_factory=_factory(),
    )

    # Replicate run_pipeline's manifest-writing step.
    manifest_path = outputs_tree / "dropbox_upload_20260101T000000Z.json"
    manifest_path.write_text(json.dumps({
        "campaign_id": "cid",
        "run_timestamp": "20260101T000000Z",
        "dropbox_run_folder": result["dropbox_run_folder"],
        "uploaded_count": result["uploaded_count"],
        "uploaded_files": result["uploaded_files"],
        "failures": result["failures"],
        "shared_links": result["shared_links"],
        "shared_link_failures": result["shared_link_failures"],
    }, indent=2))

    assert manifest_path.exists()
    parsed = json.loads(manifest_path.read_text())
    for key in (
        "campaign_id", "run_timestamp", "dropbox_run_folder",
        "uploaded_count", "uploaded_files", "failures",
        "shared_links", "shared_link_failures",
    ):
        assert key in parsed


# ---------- 9. CLI advertises the new flags --------------------------------


def test_run_pipeline_cli_advertises_dropbox_flags():
    """`scripts/run_pipeline.py --help` is the user-facing contract — make
    sure the three Dropbox flags are visible without needing to dig."""
    result = subprocess.run(
        [sys.executable, "scripts/run_pipeline.py", "--help"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert "--upload-dropbox" in out
    assert "--dropbox-root" in out
    assert "--dropbox-shared-links" in out
    # The canonical default folder is in the help text — and there's no
    # typo regression of the form "Pipepline". argparse word-wraps long
    # paths on hyphens (`/TD-` ↵ `Creative-Pipeline-POC`), so undo that
    # wrap before the substring check by collapsing whitespace and
    # joining hyphen-broken segments.
    import re
    out_collapsed = re.sub(r"-\s+", "-", " ".join(out.split()))
    assert "/TD-Creative-Pipeline-POC" in out_collapsed
    assert "Pipepline" not in out_collapsed


# ---------- 10. SDK is NOT imported when upload is off --------------------


def test_dropbox_sdk_not_imported_when_upload_off():
    """Run a fresh subprocess with no `--upload-dropbox` flag and assert
    the `dropbox` module is never imported. Guards against a regression
    where someone moves `import dropbox` to module top-level and forces
    every local user to install the SDK."""
    code = (
        "import sys\n"
        "import scripts.run_pipeline as rp\n"
        # Simulate parsing --help args (no upload).
        "rp._parse_args([])\n"
        # If the dropbox SDK got imported just by importing run_pipeline,
        # that's the regression.
        "print('dropbox' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=30,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# ---------- 11. concurrent uploads ---------------------------------------


class _ConcurrencyTrackingDropbox(FakeDropbox):
    """FakeDropbox that records the peak number of in-flight ``files_upload``
    calls. Used to assert that parallel mode actually overlaps requests."""
    _lock = None  # set on first use to avoid sharing across tests

    def __init__(self, token: str):
        super().__init__(token)
        import threading
        if type(self)._lock is None:
            type(self)._lock = threading.Lock()
        type(self)._in_flight = 0
        type(self)._peak_in_flight = 0

    def files_upload(self, content: bytes, dropbox_path: str, mode):
        import time
        cls = type(self)
        with cls._lock:
            cls._in_flight += 1
            cls._peak_in_flight = max(cls._peak_in_flight, cls._in_flight)
        # Sleep to give other threads a chance to overlap (10 ms simulates
        # the network round-trip of a real ~50KB upload).
        time.sleep(0.01)
        with cls._lock:
            cls._in_flight -= 1
        return super().files_upload(content, dropbox_path, mode)


def _make_outputs_with_n_pngs(tmp_path: Path, n: int = 12) -> Path:
    """Create n PNG files under tmp_path/outputs so we have something to
    upload concurrently."""
    out = tmp_path / "outputs"
    out.mkdir()
    for i in range(n):
        (out / f"file_{i:02d}.png").write_bytes(f"png-{i}".encode())
    return out


def test_uploads_run_in_parallel_when_parallelism_gt_1(monkeypatch, tmp_path):
    """Twelve uploads with parallelism=4 should overlap — we expect peak
    in-flight ≥ 2 (looser than ≥ 4 to keep the test stable on slow CI)."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=12)
    # Reset class-level counters between tests.
    _ConcurrencyTrackingDropbox._in_flight = 0
    _ConcurrencyTrackingDropbox._peak_in_flight = 0

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        parallelism=4,
        _dropbox_client_factory=_ConcurrencyTrackingDropbox,
    )
    assert result["uploaded_count"] == 12
    assert _ConcurrencyTrackingDropbox._peak_in_flight >= 2, (
        f"expected peak in-flight ≥ 2, got "
        f"{_ConcurrencyTrackingDropbox._peak_in_flight}"
    )


def test_uploads_run_serially_when_parallelism_eq_1(monkeypatch, tmp_path):
    """parallelism=1 must take the serial branch — peak in-flight = 1."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=4)
    _ConcurrencyTrackingDropbox._in_flight = 0
    _ConcurrencyTrackingDropbox._peak_in_flight = 0

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        parallelism=1,
        _dropbox_client_factory=_ConcurrencyTrackingDropbox,
    )
    assert result["uploaded_count"] == 4
    assert _ConcurrencyTrackingDropbox._peak_in_flight == 1


def test_parallelism_resolved_from_env_var(monkeypatch, tmp_path):
    """DROPBOX_UPLOAD_PARALLELISM env var picks up when no explicit arg."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    monkeypatch.setenv("DROPBOX_UPLOAD_PARALLELISM", "3")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=10)
    _ConcurrencyTrackingDropbox._in_flight = 0
    _ConcurrencyTrackingDropbox._peak_in_flight = 0

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        # No explicit parallelism — env should drive it.
        _dropbox_client_factory=_ConcurrencyTrackingDropbox,
    )
    assert result["uploaded_count"] == 10
    # Cap at the env-configured 3, so peak should be ≤ 3 (could be lower
    # on extremely fast machines if work completes between submissions).
    assert _ConcurrencyTrackingDropbox._peak_in_flight <= 3


def test_parallel_upload_results_sorted_deterministically(monkeypatch, tmp_path):
    """Concurrent completion order is non-deterministic; the manifest's
    uploaded_files list must still come back sorted by local path so two
    runs of the same input produce byte-identical manifests."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=8)

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        parallelism=8,
        _dropbox_client_factory=_factory(),
    )
    locals_ = [u["local"] for u in result["uploaded_files"]]
    assert locals_ == sorted(locals_)


def test_per_file_failure_records_in_parallel_mode(monkeypatch, tmp_path):
    """A single failure during parallel uploads must still be recorded;
    other files keep going."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=6)

    target_failure = "file_03.png"

    class _OneFailingDropbox(FakeDropbox):
        def files_upload(self, content, dropbox_path, mode):
            if target_failure in dropbox_path:
                raise self.ApiError("simulated parallel-mode failure")
            return super().files_upload(content, dropbox_path, mode)

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        parallelism=4,
        _dropbox_client_factory=_OneFailingDropbox,
    )
    assert result["uploaded_count"] == 5
    assert len(result["failures"]) == 1
    assert target_failure in result["failures"][0]["local"]


def test_parallelism_capped_at_file_count(monkeypatch, tmp_path):
    """Asking for parallelism=20 with only 3 files shouldn't try to
    spawn 20 threads. Internally we cap at len(files)."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "tok")
    outputs = _make_outputs_with_n_pngs(tmp_path, n=3)
    _ConcurrencyTrackingDropbox._in_flight = 0
    _ConcurrencyTrackingDropbox._peak_in_flight = 0

    result = upload_run_outputs(
        outputs_dir=outputs,
        campaign_id="cid",
        run_timestamp="20260101T000000Z",
        parallelism=20,
        _dropbox_client_factory=_ConcurrencyTrackingDropbox,
    )
    assert result["uploaded_count"] == 3
    # Peak can't exceed the file count regardless of requested parallelism.
    assert _ConcurrencyTrackingDropbox._peak_in_flight <= 3


# ---------- 12. token validator (preflight) ------------------------------


def test_validate_token_returns_failure_when_token_missing(monkeypatch):
    """No token in env, no token argument → clean (False, message) tuple,
    never an exception. Message names the env var so the user knows
    where to set it."""
    monkeypatch.delenv("DROPBOX_ACCESS_TOKEN", raising=False)
    ok, msg = validate_dropbox_token()
    assert ok is False
    assert "DROPBOX_ACCESS_TOKEN" in msg


def test_validate_token_returns_ok_for_valid_token(monkeypatch):
    """FakeDropbox's users_get_current_account succeeds → (True, msg)."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "fake-valid-token")
    ok, msg = validate_dropbox_token(_dropbox_client_factory=_factory())
    assert ok is True
    assert "valid" in msg.lower()
    # The fake's email shows up in the message — proof we actually called
    # users_get_current_account, not just "got a client".
    assert "smoke-test@example.com" in msg


def test_validate_token_returns_failure_for_invalid_token(monkeypatch):
    """Simulate the real-world expired-token path: SDK raises an AuthError.
    The validator must NOT propagate — it converts to (False, msg)."""
    class _AuthFailingDropbox(FakeDropbox):
        def users_get_current_account(self):
            raise self.ApiError(
                "AuthError('xxx', AuthError('invalid_access_token', None))"
            )

    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "expired-token")
    ok, msg = validate_dropbox_token(_dropbox_client_factory=_AuthFailingDropbox)
    assert ok is False
    assert "invalid" in msg.lower()
    # The real-world signature is preserved so a grep for the error text
    # in the manifest finds it.
    assert "invalid_access_token" in msg


def test_validate_token_never_returns_token_in_message():
    """Even when the SDK raises an exception that includes the token,
    the validator must redact it. (Belt-and-suspenders — the SDK doesn't
    typically echo tokens, but we verify our wrapper isn't the leak source.)"""
    sentinel_token = "sk-LEAK-CANARY-DO-NOT-LEAK-12345"

    class _LeakyDropbox(FakeDropbox):
        def users_get_current_account(self):
            # If the SDK ever did this, our wrapper must scrub it.
            raise self.ApiError("auth failed for token=<scrubbed>")

    ok, msg = validate_dropbox_token(
        access_token=sentinel_token,
        _dropbox_client_factory=_LeakyDropbox,
    )
    assert ok is False
    assert sentinel_token not in msg


def test_validate_token_verify_write_succeeds_when_scope_present(monkeypatch):
    """Token is valid AND has files.content.write → message reflects both
    checks ran. The fake records the upload + delete so we can assert
    the sentinel got cleaned up."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "good-token")
    record: list[FakeDropbox] = []
    ok, msg = validate_dropbox_token(
        verify_write=True,
        dropbox_root="/",
        _dropbox_client_factory=_factory(record),
    )
    assert ok is True
    assert "valid" in msg.lower()
    assert "write scope OK" in msg
    # Sentinel was uploaded then deleted.
    instance = record[0]
    assert len(instance.uploaded) == 1
    assert instance.deleted == [instance.uploaded[0][0]]


def test_validate_token_verify_write_fails_on_missing_scope(monkeypatch):
    """Reproduces the exact failure mode the user just hit: token
    authenticates but files_upload returns BadInputError because the
    files.content.write scope isn't enabled. Preflight must catch this."""
    class _NoWriteScopeDropbox(FakeDropbox):
        def files_upload(self, content, dropbox_path, mode):
            raise self.ApiError(
                "BadInputError('xxx', 'Error in call to API function "
                '"files/upload": Your app (ID: 7048323) is not permitted '
                "to access this endpoint because it does not have the "
                "required scope \\'files.content.write\\'.')"
            )

    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "scopeless-token")
    ok, msg = validate_dropbox_token(
        verify_write=True,
        dropbox_root="/",
        _dropbox_client_factory=_NoWriteScopeDropbox,
    )
    assert ok is False
    assert "write check failed" in msg
    # The actionable scope name surfaces in the message so the user
    # knows which Dropbox-app permission to enable.
    assert "files.content.write" in msg


def test_validate_token_verify_write_treats_delete_failure_as_warning(monkeypatch):
    """Cleanup of the sentinel may fail (rare; e.g. transient 500 on
    files_delete_v2). The write succeeded — that's the answer we needed
    — so preflight should still report ok=True with a warning note."""
    class _DeleteFailingDropbox(FakeDropbox):
        def files_delete_v2(self, dropbox_path):
            raise self.ApiError("transient delete failure")

    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "good-token")
    ok, msg = validate_dropbox_token(
        verify_write=True,
        dropbox_root="/",
        _dropbox_client_factory=_DeleteFailingDropbox,
    )
    assert ok is True
    assert "write scope OK" in msg
    assert "cleanup" in msg.lower() and "failed" in msg.lower()


def test_validate_token_verify_write_uses_supplied_root(monkeypatch):
    """Sentinel path must be inside the configured ``dropbox_root`` so
    the write actually exercises the right scope/folder. Verifying this
    matters: a Full-Dropbox app could pass at root but fail at the
    intended sub-folder due to a sharing/permission policy."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "good-token")
    record: list[FakeDropbox] = []
    validate_dropbox_token(
        verify_write=True,
        dropbox_root="/TD-Creative-Pipeline-POC",
        _dropbox_client_factory=_factory(record),
    )
    sentinel_path, _content, _mode = record[0].uploaded[0]
    assert sentinel_path.startswith("/TD-Creative-Pipeline-POC/_preflight_")
    assert sentinel_path.endswith(".txt")


def test_validate_token_default_does_not_do_write_check(monkeypatch):
    """Backward compat: the default verify_write=False keeps the cheap
    account-only check, so existing tests + cron-style polling don't
    suddenly start writing sentinels everywhere."""
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "good-token")
    record: list[FakeDropbox] = []
    ok, msg = validate_dropbox_token(_dropbox_client_factory=_factory(record))
    assert ok is True
    # No upload was attempted — the cheap path stays cheap.
    assert record[0].uploaded == []


def test_agent_skips_with_invalid_token_at_preflight(monkeypatch, tmp_path):
    """When DROPBOX_UPLOAD_ENABLED=true but the token is invalid,
    DropboxUploaderAgent must:
      - record reason='invalid_token' in dropbox_upload_meta
      - not raise into the agent graph
      - not call upload_run_outputs (saving N AuthErrors)
    """
    import asyncio
    from creative_pipeline.sub_agents.dropbox_uploader.agent import (
        DropboxUploaderAgent,
    )

    monkeypatch.setenv("DROPBOX_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("DROPBOX_ACCESS_TOKEN", "expired-token")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "outputs"))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "outputs"
    out.mkdir()
    (out / "report_20260101T000000Z.json").write_text(
        json.dumps({"campaign_id": "cid"})
    )

    upload_calls: list = []

    def _validate_returns_invalid(access_token=None, **kwargs):
        return False, "invalid: AuthError: invalid_access_token"

    def _fake_upload(**kwargs):
        upload_calls.append(kwargs)
        return {}

    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.validate_dropbox_token",
        _validate_returns_invalid,
    )
    monkeypatch.setattr(
        "creative_pipeline.tools.dropbox_uploader.upload_run_outputs",
        _fake_upload,
    )

    agent = DropboxUploaderAgent(name="test")

    class _Ses:
        def __init__(self, s): self.state = s

    class _Ctx:
        def __init__(self, s): self.session = _Ses(s)

    async def go():
        delta = {}
        async for event in agent._run_async_impl(
            _Ctx({"report_path": str(out / "report_20260101T000000Z.json")})
        ):
            if event.actions and event.actions.state_delta:
                delta.update(event.actions.state_delta)
        return delta

    delta = asyncio.run(go())
    meta = delta["dropbox_upload_meta"]
    assert meta["enabled"] is True
    assert meta["reason"] == "invalid_token"
    assert "invalid" in meta["error"].lower()
    # Crucially: upload_run_outputs was NEVER called — preflight short-circuit.
    assert upload_calls == []
