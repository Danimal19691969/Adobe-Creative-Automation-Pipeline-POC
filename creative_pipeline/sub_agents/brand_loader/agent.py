"""BrandLoaderAgent — parses inputs/brand/guidelines.yaml into session state."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import yaml
from google.adk.agents import BaseAgent
from google.adk.events import Event, EventActions
from google.genai import types

from creative_pipeline.schemas import BrandGuidelines

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "inputs/brand/guidelines.yaml"


class BrandLoaderAgent(BaseAgent):
    async def _run_async_impl(self, ctx):
        path = Path(os.environ.get("BRAND_GUIDELINES_PATH", _DEFAULT_PATH))
        if not path.exists():
            raise FileNotFoundError(f"Brand guidelines file not found: {path}")

        with path.open() as f:
            raw = yaml.safe_load(f)

        guidelines = BrandGuidelines.model_validate(raw)
        logger.info("Loaded brand guidelines: brand_id=%s", guidelines.brand_id)

        # Stamp run start so the reporter can compute duration_ms.
        run_started_at = datetime.now(timezone.utc).isoformat()

        yield Event(
            author=self.name,
            content=types.Content(
                role="model",
                parts=[types.Part(text=f"Loaded brand '{guidelines.brand_id}'.")],
            ),
            actions=EventActions(state_delta={
                "brand": guidelines.model_dump(mode="json"),
                "run_started_at": run_started_at,
            }),
        )


brand_loader_agent = BrandLoaderAgent(name="BrandLoaderAgent")
