"""Modular QC rule system.

Each rule implements ``QCRule.check(output_meta, image, brand) -> dict`` and
returns a ``QCRuleResult`` shape:

    {
        "name": "<rule name>",
        "passed": bool,
        "severity": "info" | "warn" | "fail",
        "details": {<rule-specific metrics>},
        "reason": "<human-readable summary>",
    }

To add a new rule (e.g. minimum-font-size, focal-area collision) drop a new
QCRule subclass into this file and add it to ``build_rules`` based on a
brand/brief flag. ``QCCheckerAgent`` runs whatever ``build_rules`` returns,
so no agent code changes when rules are added.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from PIL import Image

from creative_pipeline.schemas import BrandGuidelines
from creative_pipeline.tools.contrast import evaluate_contrast


class QCRule(ABC):
    """Abstract base. Subclasses must set ``name`` and implement ``check``."""

    name: str = "unnamed"

    @abstractmethod
    def check(self, output_meta: dict, image: Image.Image, brand: BrandGuidelines) -> dict:
        ...


class ContrastRule(QCRule):
    """WCAG contrast ratio between rendered headline color and the background
    sampled from inside ``output_meta["headline_box"]``."""

    name = "contrast_ratio"

    def __init__(
        self,
        min_ratio: float = 4.5,
        large_text_min_ratio: float = 3.0,
        large_text_size_threshold_px: int = 24,
    ):
        self.min_ratio = min_ratio
        self.large_text_min_ratio = large_text_min_ratio
        self.large_text_size_threshold_px = large_text_size_threshold_px

    def check(self, output_meta: dict, image: Image.Image, brand: BrandGuidelines) -> dict:
        box = output_meta.get("headline_box")
        text_color = output_meta.get("text_color_used")
        if not box or not text_color:
            return {
                "name": self.name,
                "passed": False,
                "severity": "fail",
                "details": {},
                "reason": "Missing headline_box or text_color_used in output metadata.",
            }

        # Decide normal vs large-text threshold from the rendered headline
        # bottom - top. Large text (≥ ~24px in our setup) gets the relaxed 3:1.
        box_height = max(1, box[3] - box[1])
        is_large_text = box_height >= self.large_text_size_threshold_px

        result = evaluate_contrast(
            image_path=output_meta["path"],
            headline_box=tuple(box),
            text_color_hex=text_color,
            min_ratio=self.min_ratio,
            is_large_text=is_large_text,
        )

        passed = bool(result["passed"])
        severity = "info" if passed else "fail"
        ratio = result["contrast_ratio"]
        if not passed:
            reason = (
                f"Contrast {ratio}:1 fails WCAG {('AA-large' if is_large_text else 'AA')} "
                f"({result['threshold']}:1) — text {result['text_color']} on "
                f"background {result['background_color']}."
            )
        else:
            reason = (
                f"Contrast {ratio}:1 passes WCAG {result['wcag_level']} "
                f"(threshold {result['threshold']}:1)."
            )

        return {
            "name": self.name,
            "passed": passed,
            "severity": severity,
            "details": result,
            "reason": reason,
        }


class DisclaimerContrastRule(QCRule):
    """WCAG contrast ratio between rendered disclaimer color and the
    background sampled from inside ``output_meta["disclaimer_box"]``.

    Treats disclaimer copy as **normal-text** for WCAG purposes (no large-text
    relaxation): regulatory text needs to clear the 4.5:1 bar regardless of
    pixel size — small legal copy is exactly when readability matters most.
    """

    name = "disclaimer_contrast"

    def __init__(self, min_ratio: float = 4.5):
        self.min_ratio = min_ratio

    def check(self, output_meta: dict, image: Image.Image, brand: BrandGuidelines) -> dict:
        # No disclaimer rendered → rule is informational pass.
        if not output_meta.get("disclaimer_text"):
            return {
                "name": self.name,
                "passed": True,
                "severity": "info",
                "details": {},
                "reason": "No disclaimer rendered for this output.",
            }

        box = output_meta.get("disclaimer_box")
        text_color = (
            output_meta.get("disclaimer_text_color")
            or output_meta.get("text_color_used")
        )
        if not box or not text_color:
            return {
                "name": self.name,
                "passed": False,
                "severity": "fail",
                "details": {},
                "reason": "Missing disclaimer_box or disclaimer_text_color in output metadata.",
            }

        from creative_pipeline.tools.contrast import evaluate_contrast

        result = evaluate_contrast(
            image_path=output_meta["path"],
            headline_box=tuple(box),  # the helper just samples this box
            text_color_hex=text_color,
            min_ratio=self.min_ratio,
            is_large_text=False,  # legal copy held to normal-text bar
        )
        passed = bool(result["passed"])
        ratio = result["contrast_ratio"]
        if passed:
            reason = (
                f"Disclaimer contrast {ratio}:1 passes "
                f"(threshold {result['threshold']}:1)."
            )
        else:
            reason = (
                f"Disclaimer contrast {ratio}:1 fails "
                f"(threshold {result['threshold']}:1) — text "
                f"{result['text_color']} on background {result['background_color']}."
            )
        return {
            "name": self.name,
            "passed": passed,
            "severity": "info" if passed else "fail",
            "details": result,
            "reason": reason,
        }


def build_rules(brand: BrandGuidelines) -> list[QCRule]:
    """Construct the active rule list from brand configuration.

    Adding a new rule = add a flag to ``RequiredBrandChecks`` (or a new
    config block) and append the matching rule here.
    """
    rules: list[QCRule] = []

    if brand.required_brand_checks.contrast_ratio:
        qc = brand.qc
        rules.append(
            ContrastRule(
                min_ratio=qc.min_contrast_ratio,
                large_text_min_ratio=qc.large_text_min_ratio,
                large_text_size_threshold_px=qc.large_text_size_threshold_px,
            )
        )

    if brand.required_brand_checks.disclaimer_contrast:
        rules.append(
            DisclaimerContrastRule(min_ratio=brand.qc.disclaimer_min_contrast_ratio)
        )

    return rules
