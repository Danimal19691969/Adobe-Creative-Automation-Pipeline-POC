"""Creative Automation Pipeline — root_agent assembly.

`adk web` discovers `root_agent` from this module. The product list is parsed
once at import time from CAMPAIGN_BRIEF_PATH; changing the brief at runtime
requires restarting `adk web` because the per-product topology is frozen here.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from google.adk.agents import ParallelAgent, SequentialAgent

from creative_pipeline.schemas import CampaignBrief
from creative_pipeline.sub_agents.asset_manager.agent import AssetManagerAgent
from creative_pipeline.sub_agents.brand_checker.agent import BrandCheckerAgent
from creative_pipeline.sub_agents.brand_loader import brand_loader_agent
from creative_pipeline.sub_agents.brief_parser import brief_parser_agent
from creative_pipeline.sub_agents.creative_composer.agent import CreativeComposerAgent
from creative_pipeline.sub_agents.dropbox_uploader import dropbox_uploader_agent
from creative_pipeline.sub_agents.image_generator.agent import ImageGeneratorAgent
from creative_pipeline.sub_agents.legal_checker.agent import LegalCheckerAgent
from creative_pipeline.sub_agents.qc_checker.agent import QCCheckerAgent
from creative_pipeline.sub_agents.reporter import reporter_agent

logger = logging.getLogger(__name__)


def _load_product_ids() -> list[str]:
    path = Path(os.environ.get("CAMPAIGN_BRIEF_PATH", "inputs/campaign_briefs/summer_refresh_2025.yaml"))
    if not path.exists():
        logger.warning("Brief at %s not found at import time; product list is empty until restart", path)
        return []
    with path.open() as f:
        raw = yaml.safe_load(f)
    brief = CampaignBrief.model_validate(raw)
    return [p.id for p in brief.products]


def _product_pipeline(product_id: str) -> SequentialAgent:
    """One sequential pipeline per product. Lives inside the per_product ParallelAgent."""
    return SequentialAgent(
        name=f"product_pipeline_{product_id}",
        sub_agents=[
            AssetManagerAgent(name=f"AssetManager_{product_id}", product_id=product_id),
            ImageGeneratorAgent(name=f"ImageGenerator_{product_id}", product_id=product_id),
            CreativeComposerAgent(name=f"CreativeComposer_{product_id}", product_id=product_id),
            BrandCheckerAgent(name=f"BrandChecker_{product_id}", product_id=product_id),
            LegalCheckerAgent(name=f"LegalChecker_{product_id}", product_id=product_id),
            QCCheckerAgent(name=f"QCChecker_{product_id}", product_id=product_id),
        ],
    )


_product_ids = _load_product_ids()
if _product_ids:
    per_product = ParallelAgent(
        name="per_product",
        sub_agents=[_product_pipeline(pid) for pid in _product_ids],
    )
    root_sub_agents = [
        brand_loader_agent, brief_parser_agent, per_product,
        reporter_agent, dropbox_uploader_agent,
    ]
else:
    # Empty brief — still let `adk web` boot so the user can fix the path.
    # Dropbox uploader stays in the chain (it's a no-op when disabled and
    # also a no-op when there's no report_path in state).
    root_sub_agents = [
        brand_loader_agent, brief_parser_agent,
        reporter_agent, dropbox_uploader_agent,
    ]

root_agent = SequentialAgent(
    name="creative_pipeline",
    sub_agents=root_sub_agents,
)
