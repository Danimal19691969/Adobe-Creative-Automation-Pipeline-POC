"""QCCheckerAgent — runs the modular QC rule set against every composed output.

Reads brand.qc thresholds + brand.required_brand_checks toggles to build the
active rule list, then executes each rule against every output produced by
``CreativeComposerAgent``. Currently runs ``ContrastRule`` (WCAG headline-vs-
background contrast); new rules drop into ``tools/qc_rules.build_rules`` with
no agent code change.

Per-output result shape:
  qc_check: {
    "summary": "pass" | "warn" | "fail",
    "rules": [<QCRuleResult>, ...],
    "headline_box": [...],
    "contrast_ratio": float,
    "wcag_level": "AAA" | "AA" | "AA-large" | "fail",
  }

Product-level summary:
  state[f"product:{pid}"]["qc_check"] = {
    "summary": "pass" | "fail",
    "failures": [<minimal failure records>],
  }

Halt behavior: when ``brief.halt_on_qc_failure`` is True and any output fails
any rule, the agent raises ``QCFailure`` after recording results so the
pipeline aborts loudly. When False, the run continues and the report carries
the failure flags.
"""

from __future__ import annotations

import logging

from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types
from PIL import Image

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.tools.qc_rules import QCRule, build_rules

logger = logging.getLogger(__name__)


class QCFailure(RuntimeError):
    """Raised when halt_on_qc_failure=True and at least one rule fails."""


def _output_qc_summary(rule_results: list[dict]) -> str:
    if any(r["severity"] == "fail" for r in rule_results):
        return "fail"
    if any(r["severity"] == "warn" for r in rule_results):
        return "warn"
    return "pass"


def _run_rules(rules: list[QCRule], output_meta: dict, brand: BrandGuidelines) -> list[dict]:
    """Open the output PNG once, run every rule against it, return results."""
    image = Image.open(output_meta["path"])
    return [rule.check(output_meta, image, brand) for rule in rules]


class QCCheckerAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brand = BrandGuidelines.model_validate(state["brand"])
        brief = CampaignBrief.model_validate(state["brief"])
        rules = build_rules(brand)

        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))
        outputs = product_state.get("outputs", [])

        if not rules:
            logger.info("QC for %s: no rules enabled; skipping.", self.product_id)
            product_state["qc_check"] = {"summary": "skipped", "failures": []}
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=(
                    f"QC for {self.product_id}: no rules enabled (skipped)."
                ))]),
                actions=EventActions(state_delta={product_key: product_state}),
            )
            return

        # Annotate outputs in place with qc results so the reporter can read them.
        annotated: list[dict] = []
        failures: list[dict] = []
        worst_summary = "pass"
        rank = {"pass": 0, "warn": 1, "fail": 2}

        for out in outputs:
            results = _run_rules(rules, out, brand)
            output_summary = _output_qc_summary(results)
            qc_block = {
                "summary": output_summary,
                "rules": results,
            }
            # Hoist contrast-rule details to the top level for easy report
            # consumption. Other rules' details stay nested in `rules`.
            for r in results:
                if r["name"] == "contrast_ratio":
                    qc_block["contrast_ratio"] = r["details"].get("contrast_ratio")
                    qc_block["wcag_level"] = r["details"].get("wcag_level")
                    qc_block["text_color"] = r["details"].get("text_color")
                    qc_block["background_color"] = r["details"].get("background_color")
                    qc_block["headline_box"] = r["details"].get("headline_box")
                elif r["name"] == "disclaimer_contrast":
                    # No disclaimer rendered → details may be empty.
                    if r["details"]:
                        qc_block["disclaimer_contrast_ratio"] = r["details"].get("contrast_ratio")
                        qc_block["disclaimer_wcag_level"] = r["details"].get("wcag_level")
                        qc_block["disclaimer_background_color"] = r["details"].get("background_color")

            new_out = {**out, "qc_check": qc_block}
            annotated.append(new_out)

            if output_summary == "fail":
                failures.append({
                    "path": out["path"],
                    "ratio": out.get("ratio"),
                    "market": out.get("market"),
                    "rule_failures": [r for r in results if not r["passed"]],
                })

            if rank[output_summary] > rank[worst_summary]:
                worst_summary = output_summary

        product_state["outputs"] = annotated
        product_state["qc_check"] = {
            "summary": worst_summary,
            "failures": failures,
            "rules_run": [r.name for r in rules],
        }

        text = (
            f"QC for {self.product_id}: {worst_summary} "
            f"({len(annotated)} outputs, {len(failures)} failure(s); "
            f"rules={[r.name for r in rules]})"
        )
        logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )

        # Halt-on-failure policy fires *after* the event is yielded so the
        # state delta still flushes — the report will record the failures
        # before the pipeline aborts.
        if brief.halt_on_qc_failure and failures:
            raise QCFailure(
                f"QC failed for {self.product_id}: {len(failures)} output(s) "
                f"violated rule(s). First failure: {failures[0]['path']}. "
                f"Halt requested via brief.halt_on_qc_failure=true."
            )


qc_checker_agent = QCCheckerAgent(name="QCCheckerAgent")
