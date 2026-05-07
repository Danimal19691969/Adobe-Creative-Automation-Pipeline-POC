"""ReportingAgent — aggregates session state into a paired set of reports.

Two complementary outputs per run:
  - ``outputs/report_{ts}.json``  — full machine-readable audit trail
    (kept verbose on purpose: every score, attempt, candidate, etc.)
  - ``outputs/report_{ts}.md``    — concise human-readable summary
    (campaign info, status table, per-product output table, only the
    fields a reviewer actually scans). The Markdown is generated from
    the same in-memory dict that becomes the JSON, so the two never
    drift apart — the JSON is the source of truth, the Markdown is a
    rendered view of it.

Convenience copies updated each run:
  - ``outputs/latest_report.json``
  - ``outputs/latest_report.md``
A reviewer who just wants "last run's results" runs ``open
outputs/latest_report.md`` regardless of timestamp.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.tools.storage_adapter import LocalStorageAdapter

logger = logging.getLogger(__name__)

_RUN_STARTED_KEY = "run_started_at"


def _started_at_or_now(state: dict) -> str:
    started = state.get(_RUN_STARTED_KEY)
    if started:
        return started
    return datetime.now(timezone.utc).isoformat()


def _wcag_short(level: str | None) -> str:
    """Compact WCAG label for table cells. ``None`` becomes ``"—"``."""
    return level if level else "—"


def _md_status_emoji(status: str | None) -> str:
    """Small visual prefix for status cells."""
    if status in (None, "n/a"):
        return "—"
    s = status.lower()
    if s == "pass":
        return "✓ pass"
    if s == "warn":
        return "⚠ warn"
    if s == "fail":
        return "✗ fail"
    return status


def _md_escape(text: str | None) -> str:
    """Escape pipe chars + collapse newlines for safe table-cell content."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ")


def _shorten_path(path: str | None, anchor: str = "outputs/") -> str:
    """Render a path relative to the ``outputs/`` root when possible, so
    Markdown tables don't get blown out by absolute filesystem paths from
    monkeypatched test runs."""
    if not path:
        return ""
    if anchor in path:
        return path[path.index(anchor):]
    return path


def _summarize_dropbox_meta(dropbox_meta: dict | None) -> tuple[str, str]:
    """Map the agent's ``dropbox_upload_meta`` shape to (status, detail)
    pair the Markdown can render. Status is one of: pass / warn / fail /
    not run / configured. Detail is a free-form string."""
    if not dropbox_meta:
        # No agent meta yet — depends on whether upload was configured.
        if os.environ.get("DROPBOX_UPLOAD_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
            return "configured", "configured (pending upload)"
        return "not run", "not configured (DROPBOX_UPLOAD_ENABLED unset)"
    if not dropbox_meta.get("enabled"):
        return "not run", "disabled (DROPBOX_UPLOAD_ENABLED unset)"
    if "dropbox_run_folder" in dropbox_meta:
        # Upload actually ran.
        failures = dropbox_meta.get("failures", 0) or 0
        uploaded = dropbox_meta.get("uploaded_count", 0) or 0
        if failures == 0:
            return "pass", (
                f"uploaded {uploaded} files → {dropbox_meta['dropbox_run_folder']}"
            )
        if uploaded == 0:
            return "fail", (
                f"0/{failures + uploaded} files uploaded → "
                f"{dropbox_meta['dropbox_run_folder']} ({failures} failures)"
            )
        return "warn", (
            f"{uploaded} uploaded, {failures} failed → "
            f"{dropbox_meta['dropbox_run_folder']}"
        )
    # Enabled but skipped (no_report_path, no_campaign_id, invalid_token, …)
    reason = dropbox_meta.get("reason", "unknown")
    error = dropbox_meta.get("error", "")
    detail = f"skipped ({reason})"
    if error:
        detail += f": {error[:120]}"
    return "fail", detail


def _summarize_warnings(report: dict) -> list[str]:
    """Collect every warning / failure string surfaced anywhere in the
    report, prefixed by source so the reviewer can grep them."""
    out: list[str] = []
    for product in report.get("products", []):
        pid = product.get("product_id", "<unknown>")
        bcs = product.get("brand_check_summary")
        if bcs in ("warn", "fail"):
            # Find the most representative reason from per-output rows.
            reasons = {
                o.get("brand_check_reason") for o in product.get("outputs", [])
                if o.get("brand_check") in ("warn", "fail")
                and o.get("brand_check_reason")
            }
            for reason in sorted(r for r in reasons if r):
                out.append(f"[brand:{pid}] {bcs}: {reason}")
        if product.get("legal_check_summary") == "fail":
            out.append(f"[legal:{pid}] failure")
        if product.get("qc_check_summary") == "fail":
            for f in product.get("qc_failures", []) or []:
                rule = f.get("rule") or f.get("name") or "unknown_rule"
                detail = f.get("reason") or f.get("detail") or ""
                out.append(f"[qc:{pid}] {rule}: {detail}".rstrip(": "))
        for w in product.get("warnings", []) or []:
            out.append(f"[product:{pid}] {w}")
        # Composition-level warnings live per-output.
        for o in product.get("outputs", []):
            cw = o.get("composition_warnings") or []
            if cw:
                short = (
                    f"{o.get('market', '?')}/{o.get('ratio', '?')}: "
                    + ", ".join(cw)
                )
                out.append(f"[composition:{pid}] {short}")
    return out


def _render_markdown_report(
    report: dict,
    dropbox_meta: dict | None = None,
    json_report_path: str | None = None,
    markdown_report_path: str | None = None,
    dropbox_manifest_path: str | None = None,
    gallery_path: str | None = None,
) -> str:
    """Render a concise human-readable Markdown view of the same dict that
    becomes ``report_<ts>.json``. JSON is the source of truth for audit;
    this is the reviewer-facing summary.

    Pure function: takes the report dict + an optional ``dropbox_meta``
    snapshot (so the dropbox agent can regenerate this Markdown with the
    actual upload result after the fact) and returns a Markdown string.
    Doesn't read from state, doesn't touch disk — easy to unit-test.
    """
    products = report.get("products", []) or []
    brief_markets = []
    brief_ratios: list[str] = []
    if products and products[0].get("outputs"):
        # The brief's markets aren't echoed at the top level of the report
        # dict, but every output row carries its market — derive the list
        # by scanning the first product (preserves brief order).
        seen: list[str] = []
        seen_ratios: list[str] = []
        for o in products[0].get("outputs", []):
            m = o.get("market")
            if m and m not in seen:
                seen.append(m)
            r = o.get("ratio")
            if r and r not in seen_ratios:
                seen_ratios.append(r)
        brief_markets = seen
        brief_ratios = seen_ratios

    total_creatives = sum(len(p.get("outputs", []) or []) for p in products)
    localized_copy = bool(report.get("localized_copy"))
    output_locales = report.get("output_locales")
    distinct_locales = sorted({
        o.get("locale") for p in products
        for o in (p.get("outputs", []) or []) if o.get("locale")
    })
    locale_count = (
        len(output_locales) if output_locales
        else (len(distinct_locales) if localized_copy else 1)
    )

    duration_s = (report.get("duration_ms") or 0) / 1000.0
    db_status, db_detail = _summarize_dropbox_meta(dropbox_meta)

    # ------- Top of file -------
    lines: list[str] = [
        "# Creative Pipeline Run Report",
        "",
        "## Run Summary",
        f"- **Campaign:** {report.get('campaign_name', '<unknown>')}",
        f"- **Campaign ID:** `{report.get('campaign_id', '<unknown>')}`",
        f"- **Brand ID:** `{report.get('brand_id', '<unknown>')}`",
        f"- **Started at:** {report.get('started_at', '<unknown>')}",
        f"- **Completed at:** {report.get('completed_at', '<unknown>')}",
        f"- **Duration:** {duration_s:.1f}s",
        f"- **Language:** `{report.get('language', '<unknown>')}` "
        f"(localized_copy=`{report.get('localized_copy')}`, "
        f"localized_legal_copy=`{report.get('localized_legal_copy')}`)",
        f"- **Markets:** {', '.join(brief_markets) or '<unknown>'}",
        f"- **Aspect ratios:** {', '.join(brief_ratios) or '<unknown>'}",
        f"- **Output locales:** "
        + (
            f"`{', '.join(output_locales)}` (explicit `output_locales` fan-out)"
            if output_locales
            else (
                f"`{', '.join(distinct_locales)}` (per-market resolution)"
                if distinct_locales else "<unknown>"
            )
        ),
        f"- **Products:** {', '.join(p.get('product_id', '?') for p in products) or '<none>'}",
        (
            f"- **Final creatives produced:** {total_creatives} "
            f"({len(products)} products × {len(brief_ratios)} aspect ratios × "
            f"{len(brief_markets)} market{'s' if len(brief_markets) != 1 else ''} × "
            f"{locale_count} locale{'s' if locale_count != 1 else ''}). "
            + (
                f"`output_locales={list(output_locales)}` — each market renders one creative "
                "per configured language variant; the single source hero per product is "
                "shared across locales (no per-locale image-gen)."
                if output_locales
                else (
                    "`localized_copy=True` — each market output uses its market-specific "
                    "locale from `campaign_message_localized`."
                    if localized_copy
                    else "`localized_copy=False` — every market renders the same selected "
                    "campaign language; market count is not reduced."
                )
            )
        ),
        f"- **Layout template:** `{report.get('layout_template', '<unknown>')}`",
        f"- **Creative quality:** `{report.get('creative_quality', '<unknown>')}`",
        f"- **Thinking provider/model:** `{report.get('provider_env', '<unset>')}` / "
        f"`{report.get('model_env', '<unset>')}`",
        f"- **Image provider:** `{report.get('image_provider_env', '<unset>')}`",
        f"- **Force generate hero:** `{report.get('force_generate_hero')}`, "
        f"**regenerate cached assets:** `{report.get('regenerate_cached_assets')}`",
        f"- **Dropbox upload:** {db_detail}",
    ]
    if gallery_path:
        lines.append(f"- **Gallery:** [{gallery_path}]({gallery_path})")

    # ------- Overall status -------
    brand_status = _worst_status(p.get("brand_check_summary") for p in products)
    legal_status = _worst_status(p.get("legal_check_summary") for p in products)
    qc_status = _worst_status(p.get("qc_check_summary") for p in products)
    lines += [
        "",
        "## Overall Status",
        "",
        "| Check | Status |",
        "|---|---|",
        f"| Brand | {_md_status_emoji(brand_status)} |",
        f"| Legal | {_md_status_emoji(legal_status)} |",
        f"| QC | {_md_status_emoji(qc_status)} |",
        f"| Dropbox upload | {_md_status_emoji(db_status)} |",
    ]

    # ------- Per-product sections -------
    lines += ["", "## Products"]
    for product in products:
        pid = product.get("product_id", "<unknown>")
        outputs = product.get("outputs", []) or []
        lines += [
            "",
            f"### `{pid}`",
            f"- **Source origin:** `{product.get('source_origin', '<unknown>')}`",
            f"- **Source asset:** `{_shorten_path(product.get('source_asset_path'))}`",
            f"- **Image provider/model:** `{product.get('image_provider', '<unset>')}` / "
            f"`{product.get('image_model', '<unset>')}`",
            f"- **Used cache:** `{product.get('used_cache')}`",
            f"- **Image gen latency:** "
            f"{product.get('image_gen_latency_ms') or 0} ms",
            f"- **Brand summary:** {_md_status_emoji(product.get('brand_check_summary'))}",
            f"- **Legal summary:** {_md_status_emoji(product.get('legal_check_summary'))}",
            f"- **QC summary:** {_md_status_emoji(product.get('qc_check_summary'))} "
            f"({len(product.get('qc_failures', []) or [])} failure(s))",
        ]
        warnings = product.get("warnings", []) or []
        if warnings:
            lines.append(
                f"- **Warnings:** {', '.join(_md_escape(w) for w in warnings)}"
            )

        # ---- Outputs table (one row per market×locale×ratio) ----
        if outputs:
            lines += [
                "",
                "| Market | Locale | Ratio | Output | Brand | Legal | QC | Headline WCAG | Disclaimer WCAG | Composition |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for o in outputs:
                composition = o.get("composition_score")
                lines.append(
                    "| {market} | {locale} | {ratio} | `{out}` | {brand} | {legal} | {qc} | "
                    "{hw} | {dw} | {comp} |".format(
                        market=_md_escape(o.get("market")),
                        locale=_md_escape(o.get("locale")),
                        ratio=_md_escape(o.get("ratio")),
                        out=_md_escape(_shorten_path(o.get("path"))),
                        brand=_md_status_emoji(o.get("brand_check")),
                        legal=_md_status_emoji(o.get("legal_check")),
                        qc=_md_status_emoji(o.get("qc_check")),
                        hw=_wcag_short(o.get("wcag_level")),
                        dw=_wcag_short(o.get("disclaimer_wcag_level")),
                        comp=(
                            f"{composition:.2f}" if isinstance(composition, (int, float))
                            else "—"
                        ),
                    )
                )

    # ------- Composition Notes (compact, only the human-relevant fields) -------
    lines += [
        "",
        "## Composition Notes",
        "",
        "Per-output summary of the fields a reviewer typically scans. "
        "Full audit data (every candidate scored, every shift attempted, "
        "QC rule details) is in the JSON report.",
    ]
    for product in products:
        pid = product.get("product_id", "<unknown>")
        for o in product.get("outputs", []) or []:
            ratio = o.get("ratio", "?")
            market = o.get("market", "?")
            lines += [
                "",
                f"**`{pid}` / {market} / {ratio}** — `{_shorten_path(o.get('path'))}`",
                f"  - Headline: {o.get('headline_size_px') or '—'} px, "
                f"{o.get('headline_line_count') or '—'} line(s), "
                f"color `{o.get('headline_color_selected') or '—'}`, "
                f"prominence {(o.get('headline_prominence_score') or 0):.2f}",
                f"  - Headline box: {_md_escape(o.get('headline_box_selected_pct'))}",
                f"  - Logo: position `{o.get('logo_position_selected') or '—'}` "
                f"(configured `{o.get('logo_position_configured') or '—'}`, "
                f"adjusted={o.get('logo_position_adjusted')}, "
                f"gap={o.get('logo_product_gap_px') or '—'} px)",
                f"  - Disclaimer: position `{o.get('disclaimer_position_selected') or '—'}`, "
                f"contrast {(o.get('disclaimer_contrast_ratio') or 0):.2f} "
                f"({_wcag_short(o.get('disclaimer_wcag_level'))})",
                f"  - Headline contrast: "
                f"{(o.get('contrast_ratio') or 0):.2f} ({_wcag_short(o.get('wcag_level'))})",
                f"  - Composition score: "
                f"{(o.get('composition_score') or 0):.2f}"
                + (
                    f" — warnings: {', '.join(o.get('composition_warnings') or [])}"
                    if o.get("composition_warnings") else ""
                ),
            ]

    # ------- Failures and warnings -------
    issues = _summarize_warnings(report)
    if dropbox_meta and dropbox_meta.get("failures"):
        issues.append(
            f"[dropbox] {dropbox_meta['failures']} per-file upload failure(s) — "
            f"see manifest"
        )
    lines += ["", "## Failures and Warnings"]
    if issues:
        for issue in issues:
            lines.append(f"- {_md_escape(issue)}")
    else:
        lines.append("_No blocking failures._")

    # ------- Files -------
    lines += ["", "## Files"]
    if json_report_path:
        lines.append(f"- **JSON report:** `{_shorten_path(json_report_path)}`")
    if markdown_report_path:
        lines.append(f"- **Markdown report:** `{_shorten_path(markdown_report_path)}`")
    lines += [
        "- **Latest copies:** `outputs/latest_report.json`, `outputs/latest_report.md`",
    ]
    if dropbox_manifest_path:
        lines.append(
            f"- **Dropbox upload manifest:** `{_shorten_path(dropbox_manifest_path)}`"
        )
    if gallery_path:
        lines.append(f"- **Gallery:** `{_shorten_path(gallery_path)}`")

    lines.append("")  # trailing newline
    return "\n".join(lines)


def _worst_status(values) -> str:
    """``fail > warn > pass > n/a`` semantics for an iterable of strings."""
    rank = {"pass": 0, "n/a": 0, None: 0, "warn": 1, "fail": 2}
    seen = list(values)
    if not seen:
        return "n/a"
    return max(seen, key=lambda v: rank.get(v, 0)) or "n/a"


def _write_report_pair(
    output_dir: str,
    timestamp: str,
    report: dict,
    dropbox_meta: dict | None = None,
    dropbox_manifest_path: str | None = None,
) -> tuple[str, str]:
    """Persist the JSON + Markdown pair AND refresh the
    ``latest_report.{json,md}`` convenience copies. Returns the two
    timestamped paths. Used by both the Reporter (initial write) and
    the Dropbox uploader (re-render with upload result)."""
    json_path = f"{output_dir}/report_{timestamp}.json"
    md_path = f"{output_dir}/report_{timestamp}.md"

    storage = LocalStorageAdapter()
    storage.write(json_path, json.dumps(report, indent=2).encode("utf-8"))

    markdown = _render_markdown_report(
        report,
        dropbox_meta=dropbox_meta,
        json_report_path=json_path,
        markdown_report_path=md_path,
        dropbox_manifest_path=dropbox_manifest_path,
    )
    storage.write(md_path, markdown.encode("utf-8"))

    # Convenience: ``open outputs/latest_report.md`` always shows the
    # last run regardless of timestamp. shutil.copy is cross-platform
    # safe (no symlink semantics that break on Windows demo machines).
    out = Path(output_dir)
    try:
        shutil.copy(json_path, out / "latest_report.json")
        shutil.copy(md_path, out / "latest_report.md")
    except OSError as e:
        logger.warning("Could not refresh latest_report.{json,md}: %s", e)

    return json_path, md_path


def _derive_source_origin(product_state: dict) -> str:
    """Unambiguous label for where the source hero came from this run.

    Possible values (from existing per-product state, no new state shape):
      - "generated_this_run"            — ImageGeneratorAgent fired the API this run
      - "reused_generated_previous_run" — AssetManager loaded a prior outputs/{pid}/source/* file
      - "reused_local_placeholder"      — AssetManager loaded inputs/assets/{pid}/hero.*
      - "no_source"                     — neither path produced a hero (failure mode)
    """
    asset_source = product_state.get("asset_source")
    used_cache = product_state.get("used_cache")
    if asset_source == "user_supplied":
        return "reused_local_placeholder"
    if asset_source in ("openai_generated", "imagen_generated"):
        return "reused_generated_previous_run" if used_cache else "generated_this_run"
    if asset_source == "generated_cached":
        return "reused_generated_previous_run"
    if asset_source is None and product_state.get("hero_path") is None:
        return "no_source"
    return "unknown"


class ReportingAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brief = state.get("brief", {}) or {}
        product_ids: list[str] = state.get("product_ids", [])

        completed_at = datetime.now(timezone.utc)
        started_at_iso = _started_at_or_now(state)
        try:
            duration_ms = int(
                (completed_at - datetime.fromisoformat(started_at_iso)).total_seconds() * 1000
            )
        except ValueError:
            duration_ms = 0

        # Provider env snapshot — captured at report time, not run-start time.
        provider_env = os.environ.get("PROVIDER", "<unset>")
        model_env = os.environ.get("MODEL", "<unset>")
        image_provider_env = os.environ.get("IMAGE_PROVIDER") or provider_env

        products_report: list[dict] = []
        for pid in product_ids:
            ps = state.get(f"product:{pid}", {}) or {}
            outputs = ps.get("outputs", [])
            brand_check = ps.get("brand_check", {}) or {}
            legal_check = ps.get("legal_check", {}) or {}
            per_brand = {item["path"]: item for item in brand_check.get("per_output", [])}
            qc_check = ps.get("qc_check", {}) or {}
            source_origin = _derive_source_origin(ps)

            output_records = []
            for o in outputs:
                bc = per_brand.get(o.get("path"), {})
                qc = o.get("qc_check", {}) or {}
                output_records.append({
                    "market": o.get("market"),
                    "locale": o.get("locale"),
                    "ratio": o.get("ratio"),
                    "path": o.get("path"),
                    "headline": o.get("headline"),
                    "disclaimer_text": o.get("disclaimer_text"),
                    # Per-output provenance, mirrored from product-level state so each
                    # output row is self-contained for downstream consumers.
                    "source_asset_path": ps.get("hero_path"),
                    "source_origin": source_origin,
                    "asset_source": ps.get("asset_source"),
                    "image_provider": ps.get("image_provider"),
                    "image_model": ps.get("image_model"),
                    "used_cache": ps.get("used_cache"),
                    "force_generate_hero": brief.get("force_generate_hero"),
                    "regenerate_cached_assets": brief.get("regenerate_cached_assets"),
                    "language": brief.get("language"),
                    "localized_copy": brief.get("localized_copy"),
                    "localized_legal_copy": brief.get("localized_legal_copy"),
                    "layout_template": brief.get("layout_template"),
                    "creative_quality": brief.get("creative_quality"),
                    # Layout accounting from the composer.
                    "headline_box": o.get("headline_box"),
                    "disclaimer_box": o.get("disclaimer_box"),
                    "logo_box": o.get("logo_box"),
                    "logo_position": o.get("logo_position"),
                    "logo_size_px": o.get("logo_size_px"),
                    "logo_treatment": o.get("logo_treatment"),
                    "overlay_style": o.get("overlay_style"),
                    "overlay_opacity": o.get("overlay_opacity"),
                    "accent_style": o.get("accent_style"),
                    "accent_color": o.get("accent_color"),
                    # Final rendered type sizes — driven by brand.typography.per_aspect.
                    "headline_size_px": o.get("headline_size_px"),
                    "headline_line_count": o.get("headline_line_count"),
                    "disclaimer_size_px": o.get("disclaimer_size_px"),
                    # Local readability treatments (panel behind headline,
                    # badge behind disclaimer) and headline zone-fill telemetry.
                    "headline_background_treatment": o.get("headline_background_treatment"),
                    "headline_text_shadow": o.get("headline_text_shadow"),
                    "headline_zone_fill_pct": o.get("headline_zone_fill_pct"),
                    "disclaimer_background_treatment": o.get("disclaimer_background_treatment"),
                    # Photographic-composition + fallback telemetry.
                    "image_composition_guidance_used": o.get("image_composition_guidance_used"),
                    "negative_space_location": o.get("negative_space_location"),
                    "readability_fallback_used": o.get("readability_fallback_used"),
                    "composer_contrast_estimate": o.get("composer_contrast_estimate"),
                    "post_treatment_contrast_estimate": o.get("post_treatment_contrast_estimate"),
                    # Headline-zone selection audit (text-safe-area scoring).
                    "headline_box_selected": o.get("headline_box_selected_pct"),
                    "headline_box_selected_pct": o.get("headline_box_selected_pct"),
                    "headline_box_selected_px": o.get("headline_box_selected_px"),
                    "headline_selection_reason": o.get("headline_box_selection_reason"),
                    "headline_box_selection_reason": o.get("headline_box_selection_reason"),
                    "headline_box_score": o.get("headline_box_score"),
                    "headline_box_scores": o.get("headline_box_candidates"),
                    "headline_region_texture_score": o.get("headline_region_texture_score"),
                    "headline_region_edge_density": o.get("headline_region_edge_density"),
                    "headline_region_contrast_estimate": o.get("headline_region_contrast_estimate"),
                    "headline_color_selected": o.get("headline_color_selected"),
                    "headline_color_candidates": o.get("headline_color_candidates"),
                    "headline_color_selection_reason": o.get("headline_color_selection_reason"),
                    "headline_wrap_variant": o.get("headline_wrap_variant"),
                    "headline_scale_reason": o.get("headline_scale_reason"),
                    "headline_fit_status": o.get("headline_fit_status"),
                    "headline_font_size_px": o.get("headline_font_size_px"),
                    # Rendered text bbox — the actual on-canvas text area
                    # (post-wrap, post-alignment), distinct from the wider
                    # candidate headline_box. QC and the prominence score
                    # both sample under this bbox.
                    "rendered_headline_bbox": o.get("rendered_headline_bbox"),
                    "rendered_headline_bbox_pct": o.get("rendered_headline_bbox_pct"),
                    # Prominence score (weighted: size + zone fill + line +
                    # fit + contrast + clearance). Components surfaced too
                    # so reviewers can see which factor dominates a low score.
                    "headline_prominence_score": o.get("headline_prominence_score"),
                    "headline_size_factor": o.get("headline_size_factor"),
                    "headline_zone_fill_factor": o.get("headline_zone_fill_factor"),
                    "headline_line_factor": o.get("headline_line_factor"),
                    "headline_fit_factor": o.get("headline_fit_factor"),
                    "headline_contrast_factor": o.get("headline_contrast_factor"),
                    "headline_clearance_factor": o.get("headline_clearance_factor"),
                    "headline_max_size_px_configured": o.get("headline_max_size_px_configured"),
                    "headline_min_size_px_configured": o.get("headline_min_size_px_configured"),
                    "headline_target_h_px": o.get("headline_target_h_px"),
                    "headline_target_zone_fill_pct": o.get("headline_target_zone_fill_pct"),
                    "headline_box_candidates": o.get("headline_box_candidates"),
                    # Focal-area / product safe-zone audit. ``text_object_*``
                    # fields reflect the *rendered* text bbox vs the focal
                    # safe zone — not the candidate box. Collision and
                    # near-miss are both composition failures even when no
                    # direct overlap exists.
                    "focal_area_estimate": o.get("focal_area_estimate"),
                    "product_safe_zone_box": o.get("product_safe_zone_box"),
                    "expanded_product_safe_zone_box": o.get("expanded_product_safe_zone_box"),
                    "focal_overlap_detected": o.get("focal_overlap_detected"),
                    "focal_near_miss_detected": o.get("focal_near_miss_detected"),
                    "focal_overlap_pct": o.get("focal_overlap_pct"),
                    "text_object_gap_px": o.get("text_object_gap_px"),
                    "text_object_clearance_pass": o.get("text_object_clearance_pass"),
                    "text_object_collision_detected": o.get("text_object_collision_detected"),
                    "text_object_near_miss_detected": o.get("text_object_near_miss_detected"),
                    "all_candidates_failed_clearance": o.get("all_candidates_failed_clearance"),
                    "clearance_failure_reason": o.get("clearance_failure_reason"),
                    "min_text_object_gap_px_threshold": o.get("min_text_object_gap_px_threshold"),
                    # Headline-box adjustment audit. Includes every shift the
                    # cascade tried (accepted or not) and the final box.
                    "headline_box_original": o.get("headline_box_original"),
                    "headline_box_adjusted": o.get("headline_box_adjusted"),
                    "headline_box_adjustment_reason": o.get("headline_box_adjustment_reason"),
                    "headline_box_shift_attempts": o.get("headline_box_shift_attempts"),
                    "headline_box_shift_success": o.get("headline_box_shift_success"),
                    # Disclaimer clearance audit.
                    "disclaimer_text_object_gap_px": o.get("disclaimer_text_object_gap_px"),
                    "disclaimer_clearance_pass": o.get("disclaimer_clearance_pass"),
                    "disclaimer_position_selected": o.get("disclaimer_position_selected"),
                    "disclaimer_position_configured": o.get("disclaimer_position_configured"),
                    "disclaimer_candidate_index": o.get("disclaimer_candidate_index"),
                    "disclaimer_candidate_attempts": o.get("disclaimer_candidate_attempts"),
                    "disclaimer_selection_reason": o.get("disclaimer_selection_reason"),
                    # Crop / anti-clip audit.
                    "crop_strategy_used": o.get("crop_strategy_used"),
                    "crop_box_used": o.get("crop_box_used"),
                    "crop_box_candidates": o.get("crop_box_candidates"),
                    "crop_box_scores": o.get("crop_box_scores"),
                    "focal_edge_gap_px": o.get("focal_edge_gap_px"),
                    "focal_edge_min_gap_px": o.get("focal_edge_min_gap_px"),
                    "focal_edge_clearance_pass": o.get("focal_edge_clearance_pass"),
                    "focal_edge_clip_detected": o.get("focal_edge_clip_detected"),
                    "focal_edges_touched": o.get("focal_edges_touched"),
                    "crop_edge_clip_penalty_applied": o.get("crop_edge_clip_penalty_applied"),
                    # Letterbox fallback audit.
                    "letterbox_applied": o.get("letterbox_applied"),
                    "letterbox_pad_pct": o.get("letterbox_pad_pct"),
                    "letterbox_color_used": o.get("letterbox_color_used"),
                    "letterbox_color_source": o.get("letterbox_color_source"),
                    # Headline wrap + adaptive widening audit.
                    "headline_wrap_strategy": o.get("headline_wrap_strategy"),
                    "headline_box_widened": o.get("headline_box_widened"),
                    "headline_box_width_delta_pct": o.get("headline_box_width_delta_pct"),
                    "headline_box_pre_widen_pct": o.get("headline_box_pre_widen_pct"),
                    "headline_widen_reason": o.get("headline_widen_reason"),
                    # Logo placement audit.
                    "logo_position_selected": o.get("logo_position_selected"),
                    "logo_position_configured": o.get("logo_position_configured"),
                    "logo_position_adjusted": o.get("logo_position_adjusted"),
                    "logo_product_gap_px": o.get("logo_product_gap_px"),
                    "logo_product_clearance_pass": o.get("logo_product_clearance_pass"),
                    "logo_collision_detected": o.get("logo_collision_detected"),
                    "logo_selection_reason": o.get("logo_selection_reason"),
                    "logo_position_attempts": o.get("logo_position_attempts"),
                    "logo_min_required_gap_px": o.get("logo_min_required_gap_px"),
                    # Accent safe-zone audit.
                    "accent_line_box": o.get("accent_line_box"),
                    "accent_edge_gap_px": o.get("accent_edge_gap_px"),
                    "accent_safe_zone_pass": o.get("accent_safe_zone_pass"),
                    # Composition score: aggregate health for this output
                    # plus a machine-readable warning list.
                    "composition_score": o.get("composition_score"),
                    "composition_warnings": o.get("composition_warnings"),
                    "composition_factors": o.get("composition_factors"),
                    # Disclaimer-contrast QC (set when required_brand_checks.disclaimer_contrast is true).
                    "disclaimer_contrast_ratio": qc.get("disclaimer_contrast_ratio"),
                    "disclaimer_wcag_level": qc.get("disclaimer_wcag_level"),
                    "disclaimer_background_sample_color": qc.get("disclaimer_background_color"),
                    # Brand check, with scores + reason.
                    "brand_check": bc.get("status", "n/a"),
                    "brand_check_reason": bc.get("brand_check_reason"),
                    "brand_palette_score": bc.get("brand_palette_score"),
                    "brand_element_score": bc.get("brand_element_score"),
                    "legal_check": legal_check.get("summary", "n/a"),
                    # QC (WCAG contrast and any future modular rules).
                    "qc_check": qc.get("summary", "n/a"),
                    "contrast_ratio": qc.get("contrast_ratio"),
                    "wcag_level": qc.get("wcag_level"),
                    "text_color": qc.get("text_color") or o.get("text_color_used"),
                    "background_color": qc.get("background_color"),
                    "qc_rules": qc.get("rules"),
                })

            products_report.append({
                "product_id": pid,
                "asset_source": ps.get("asset_source"),
                "source_asset_path": ps.get("hero_path"),
                # Unambiguous label: generated_this_run | reused_generated_previous_run
                # | reused_local_placeholder | no_source.
                "source_origin": source_origin,
                "image_provider": ps.get("image_provider"),
                "image_model": ps.get("image_model"),
                "image_gen_latency_ms": ps.get("image_gen_latency_ms"),
                "used_cache": ps.get("used_cache"),
                "outputs": output_records,
                "brand_check_summary": brand_check.get("summary", "n/a"),
                "legal_check_summary": legal_check.get("summary", "n/a"),
                "qc_check_summary": qc_check.get("summary", "n/a"),
                "qc_failures": qc_check.get("failures", []),
                "qc_rules_run": qc_check.get("rules_run", []),
                "warnings": ps.get("warnings", []),
            })

        report = {
            "campaign_id": brief.get("campaign_id"),
            "campaign_name": brief.get("campaign_name"),
            "brand_id": brief.get("brand_id"),
            "started_at": started_at_iso,
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "language": brief.get("language"),
            "localized_copy": brief.get("localized_copy"),
            "localized_legal_copy": brief.get("localized_legal_copy"),
            "output_locales": brief.get("output_locales"),
            "force_generate_hero": brief.get("force_generate_hero"),
            "regenerate_cached_assets": brief.get("regenerate_cached_assets"),
            "creative_quality": brief.get("creative_quality"),
            "layout_template": brief.get("layout_template"),
            "provider_env": provider_env,
            "model_env": model_env,
            "image_provider_env": image_provider_env,
            "products": products_report,
        }

        timestamp = completed_at.strftime("%Y%m%dT%H%M%SZ")
        output_dir = os.environ.get("OUTPUT_DIR", "outputs")

        # Write the JSON + Markdown pair, plus refresh the
        # ``latest_report.{json,md}`` convenience copies. Markdown shows
        # Dropbox upload status as "configured (pending)" or "disabled"
        # depending on env at write time — the DropboxUploaderAgent
        # regenerates this Markdown after upload completes so the final
        # version reflects the actual upload result.
        json_path, md_path = _write_report_pair(
            output_dir=output_dir,
            timestamp=timestamp,
            report=report,
            dropbox_meta=None,  # uploader, if it runs, regenerates with real meta
        )
        logger.info("Wrote report: %s", json_path)
        logger.info("Wrote markdown summary: %s", md_path)

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=(
                    f"Wrote run report → {json_path}\n"
                    f"Wrote markdown summary → {md_path}"
                ))],
            ),
            actions=EventActions(state_delta={
                "report_path": json_path,
                "markdown_report_path": md_path,
                "report_timestamp": timestamp,
            }),
        )


reporter_agent = ReportingAgent(name="ReportingAgent")
