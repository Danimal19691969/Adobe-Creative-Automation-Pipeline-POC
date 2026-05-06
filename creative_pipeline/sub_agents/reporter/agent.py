"""ReportingAgent — aggregates session state into outputs/report_{ISO8601}.json."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

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


class ReportingAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brief = state.get("brief", {})
        product_ids: list[str] = state.get("product_ids", [])

        completed_at = datetime.now(timezone.utc)
        started_at_iso = _started_at_or_now(state)
        try:
            duration_ms = int(
                (completed_at - datetime.fromisoformat(started_at_iso)).total_seconds() * 1000
            )
        except ValueError:
            duration_ms = 0

        products_report: list[dict] = []
        for pid in product_ids:
            ps = state.get(f"product:{pid}", {})
            outputs = ps.get("outputs", [])
            brand_check = ps.get("brand_check", {})
            legal_check = ps.get("legal_check", {})
            per_brand = {item["path"]: item["status"] for item in brand_check.get("per_output", [])}
            output_records = [
                {
                    "market": o.get("market"),
                    "locale": o.get("locale"),
                    "ratio": o.get("ratio"),
                    "path": o.get("path"),
                    "brand_check": per_brand.get(o.get("path"), "n/a"),
                    "legal_check": legal_check.get("summary", "n/a"),
                }
                for o in outputs
            ]
            products_report.append({
                "product_id": pid,
                "asset_source": ps.get("asset_source"),
                "image_model": ps.get("image_model"),
                "image_gen_latency_ms": ps.get("image_gen_latency_ms"),
                "outputs": output_records,
                "brand_check_summary": brand_check.get("summary", "n/a"),
                "legal_check_summary": legal_check.get("summary", "n/a"),
                "warnings": ps.get("warnings", []),
            })

        report = {
            "campaign_id": brief.get("campaign_id"),
            "brand_id": brief.get("brand_id"),
            "started_at": started_at_iso,
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "products": products_report,
        }

        timestamp = completed_at.strftime("%Y%m%dT%H%M%SZ")
        output_dir = os.environ.get("OUTPUT_DIR", "outputs")
        report_path = f"{output_dir}/report_{timestamp}.json"
        LocalStorageAdapter().write(
            report_path, json.dumps(report, indent=2).encode("utf-8")
        )
        logger.info("Wrote report: %s", report_path)

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Wrote run report → {report_path}")],
            ),
            actions=EventActions(state_delta={"report_path": report_path}),
        )


reporter_agent = ReportingAgent(name="ReportingAgent")
