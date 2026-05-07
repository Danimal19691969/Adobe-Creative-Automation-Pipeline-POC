"""ImageGeneratorAgent — LiteLLM-backed agent that generates a hero image.

Activation gate:
  - If the asset manager already located a hero (user-supplied or cached
    generation), skip the LLM and the image API entirely.
  - Otherwise build the prompt, delegate to the LiteLLM-backed inner LlmAgent
    which calls the generate_hero_image tool, and persist the result to
    outputs/{pid}/source/global_{ts}.png with a sidecar JSON capturing
    asset_source / image_model / image_provider / image_gen_latency_ms so
    subsequent runs (with regenerate_cached_assets=False) can reuse it.

The cache wrapper around an LlmAgent keeps the LlmAgent + before-model-callback
shape (legal pre-check halts on prohibited words) while making the cache gate
deterministic — the LLM has nothing useful to add to a binary cache hit/miss.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.events import Event, EventActions
from google.adk.runners import InMemoryRunner
from google.genai import types

from creative_pipeline.schemas import BrandGuidelines, CampaignBrief
from creative_pipeline.sub_agents.image_generator.prompts import build_prompt
from creative_pipeline.sub_agents.legal_checker.agent import legal_precheck_callback
from creative_pipeline.tools.image_gen import generate_hero_image
from creative_pipeline.tools.llm_factory import make_llm

logger = logging.getLogger(__name__)


def _build_inner_llm_agent(product_id: str, hero_aspect_ratio: str) -> LlmAgent:
    return LlmAgent(
        name=f"ImageGenLLM_{product_id}",
        model=make_llm(),
        instruction=(
            "You are responsible for generating a single hero image for a marketing "
            "campaign. The user message contains the prompt, the output path, and "
            "the aspect ratio. Call the generate_hero_image tool exactly once with "
            "prompt, out_path, and aspect_ratio set to those values. Do not call "
            "any other tool. After the tool returns, reply with a one-sentence "
            "confirmation. The aspect ratio for this run is "
            f"'{hero_aspect_ratio}'."
        ),
        tools=[generate_hero_image],
        before_model_callback=legal_precheck_callback,
    )


def _source_paths(product_id: str) -> tuple[Path, Path, str]:
    """(out_path, sidecar_path, timestamp) for a fresh generation."""
    output_dir = os.environ.get("OUTPUT_DIR", "outputs")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(output_dir) / product_id / "source"
    out_path = out_dir / f"global_{ts}.png"
    sidecar = out_path.with_suffix(".json")
    return out_path, sidecar, ts


class ImageGeneratorAgent(BaseAgent):
    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))

        # Cache hit (asset manager found a user-supplied or previously-generated hero).
        if product_state.get("hero_path"):
            text = (
                f"[cache hit] reusing hero for {self.product_id} "
                f"(asset_source={product_state.get('asset_source')}): "
                f"{product_state['hero_path']}"
            )
            logger.info(text)
            yield Event(
                author=self.name,
                content=types.Content(role="model", parts=[types.Part(text=text)]),
            )
            return

        # Cache miss — build prompt and delegate to the LiteLLM-backed inner agent.
        brand = BrandGuidelines.model_validate(state["brand"])
        brief = CampaignBrief.model_validate(state["brief"])
        product = next((p for p in brief.products if p.id == self.product_id), None)
        if product is None:
            raise RuntimeError(f"Product {self.product_id!r} not found in brief")

        prompt = build_prompt(product, brief, brand)
        out_path, sidecar_path, ts = _source_paths(self.product_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Hero generated at the first declared aspect ratio (default 1x1).
        hero_aspect = next(iter(brand.aspect_ratios.keys()), "1x1")
        hero_aspect_api = hero_aspect.replace("x", ":")

        # OpenAI quality knob is sourced from creative_quality. We thread it via env
        # because the tool function signature is shared across providers; the openai
        # tool reads OPENAI_IMAGE_QUALITY and ignores anything Imagen doesn't support.
        os.environ["OPENAI_IMAGE_QUALITY"] = brief.creative_quality.value

        inner_agent = _build_inner_llm_agent(self.product_id, hero_aspect_api)
        runner = InMemoryRunner(agent=inner_agent, app_name=f"image_gen_{self.product_id}")
        sub_session = await runner.session_service.create_session(
            app_name=f"image_gen_{self.product_id}",
            user_id="pipeline",
            state={"brief": state["brief"], "brand": state["brand"]},
        )

        user_msg = types.Content(
            role="user",
            parts=[types.Part(text=(
                f"prompt: {prompt}\n"
                f"out_path: {out_path}\n"
                f"aspect_ratio: {hero_aspect_api}"
            ))],
        )

        tool_result: dict | None = None
        async for inner_event in runner.run_async(
            user_id="pipeline", session_id=sub_session.id, new_message=user_msg
        ):
            if inner_event.content and inner_event.content.parts:
                for part in inner_event.content.parts:
                    fn_response = getattr(part, "function_response", None)
                    if fn_response and getattr(fn_response, "name", None) == "generate_hero_image":
                        response_payload = getattr(fn_response, "response", None) or {}
                        if isinstance(response_payload, dict):
                            tool_result = response_payload.get("result", response_payload)

        if tool_result is None:
            raise RuntimeError(
                f"ImageGeneratorAgent: inner LLM did not call generate_hero_image for {self.product_id}"
            )

        # Persist sidecar so the next run with regenerate_cached_assets=False can
        # surface the same provenance via AssetManagerAgent.
        sidecar_payload = {
            "asset_source": tool_result.get("asset_source"),
            "image_model": tool_result.get("model"),
            "image_provider": tool_result.get("provider"),
            "image_gen_latency_ms": tool_result.get("latency_ms"),
            "generated_at": ts,
            "prompt": prompt,
        }
        sidecar_path.write_text(json.dumps(sidecar_payload, indent=2))

        product_state["hero_path"] = tool_result["path"]
        product_state["asset_source"] = tool_result.get("asset_source")
        product_state["image_model"] = tool_result.get("model")
        product_state["image_provider"] = tool_result.get("provider")
        product_state["image_gen_latency_ms"] = tool_result.get("latency_ms")
        product_state["used_cache"] = False

        text = (
            f"Generated hero for {self.product_id}: {tool_result['path']} "
            f"({tool_result.get('asset_source')}, {tool_result.get('model')}, "
            f"{tool_result.get('latency_ms')}ms)"
        )
        logger.info(text)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


image_generator_agent = ImageGeneratorAgent(name="ImageGeneratorAgent")
