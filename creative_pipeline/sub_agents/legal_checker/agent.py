"""LegalCheckerAgent + the before_model_callback used to gate image generation.

Two responsibilities:
  1. ``legal_precheck_callback`` — a ``before_model_callback`` for the
     ImageGeneratorAgent. Scans ``brief.campaign_message`` for prohibited words
     from ``brand.legal.prohibited_words``. Raises if any hit so the pipeline
     halts before the image-gen API is called.
  2. ``LegalCheckerAgent`` — a per-product agent that runs after composition
     and verifies that, for every (market, locale) output, the disclaimer was
     rendered when the brand requires one for that market.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from google.adk.agents import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.events import Event, EventActions
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types

logger = logging.getLogger(__name__)


class LegalViolation(RuntimeError):
    """Raised when a campaign message contains a brand-prohibited word."""


def _scan_prohibited_words(messages: dict[str, str], prohibited: list[str]) -> list[tuple[str, str, str]]:
    """Return a list of (locale, word, message) for every prohibited-word hit."""
    hits: list[tuple[str, str, str]] = []
    for word in prohibited:
        # Word-boundary, case-insensitive
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        for locale, msg in messages.items():
            if pattern.search(msg):
                hits.append((locale, word, msg))
    return hits


def _collect_messages(brief: dict) -> dict[str, str]:
    """Pull every piece of rendered copy out of the brief into a {label: text} dict
    so they can be regex-scanned together. Handles the current schema
    (campaign_message: str, campaign_message_localized: dict) and the legacy
    shape (campaign_message: dict)."""
    out: dict[str, str] = {}
    cm = brief.get("campaign_message")
    if isinstance(cm, str):
        out["primary"] = cm
    elif isinstance(cm, dict):
        out.update({f"primary[{k}]": v for k, v in cm.items()})
    localized = brief.get("campaign_message_localized") or {}
    if isinstance(localized, dict):
        out.update({f"localized[{k}]": v for k, v in localized.items()})
    # Per-product overrides
    for product in brief.get("products", []) or []:
        msg = product.get("campaign_message")
        if isinstance(msg, str) and msg:
            out[f"product[{product.get('id', '?')}]"] = msg
    return out


def legal_precheck_callback(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Halt image generation if any campaign message contains a prohibited word."""
    state = callback_context.state
    brief = state.get("brief")
    brand = state.get("brand")
    if not brief or not brand:
        return None

    prohibited = brand.get("legal", {}).get("prohibited_words", [])
    messages = _collect_messages(brief)
    hits = _scan_prohibited_words(messages, prohibited)
    if hits:
        details = "; ".join(f"{loc!r}={word!r} in {msg!r}" for loc, word, msg in hits)
        raise LegalViolation(
            f"Prohibited word(s) detected in campaign messages — halting before image generation: {details}"
        )
    logger.info("legal_precheck_callback: no prohibited words found")
    return None


class LegalCheckerAgent(BaseAgent):
    """Post-composition disclaimer verifier per product."""

    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        brand = state.get("brand", {})
        required = brand.get("legal", {}).get("required_disclaimers", {})

        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))
        outputs = product_state.get("outputs", [])

        failures: list[str] = []
        for out in outputs:
            market = out.get("market")
            disclaimer_for_market = required.get(market)
            if disclaimer_for_market and not out.get("disclaimer_rendered"):
                failures.append(
                    f"{market} requires disclaimer but {out.get('path')} did not render it"
                )

        check = "pass" if not failures else "fail"
        product_state["legal_check"] = {"summary": check, "failures": failures}

        text = (
            f"Legal post-check for {self.product_id}: {check}"
            + ("" if not failures else f" — {len(failures)} failure(s)")
        )
        logger.info(text)

        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


legal_checker_agent = LegalCheckerAgent(name="LegalCheckerAgent")
