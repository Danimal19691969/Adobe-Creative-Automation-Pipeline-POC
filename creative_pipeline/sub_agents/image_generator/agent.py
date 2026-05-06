"""ImageGeneratorAgent — LiteLLM-backed agent that generates a hero image.

Activation gate (per spec §7.4): only fires the underlying LLM + image-gen tool
when ``state['product:{pid}']['hero_path']`` is None. If the asset manager
already located a cached hero, this agent emits a no-op event.

Architecture:

  ImageGeneratorAgent (BaseAgent, per-product wrapper)
    - cache check (skip if hero_path is set)
    - delegates to inner LlmAgent (LiteLLM-backed, via make_llm())
        - tool: generate_hero_image (image-provider dispatcher)
        - before_model_callback: legal_precheck_callback (halts on prohibited words)
    - records image-gen result + asset_source = "generated" into state

The cache-check wrapper around an LlmAgent keeps the LlmAgent + before-model
callback shape from spec §7.4 while making the gate deterministic instead of
LLM-decided (the LLM has nothing useful to add to a binary cache hit/miss).
"""

from __future__ import annotations

import logging
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


def _build_inner_llm_agent(product_id: str) -> LlmAgent:
    """Per-product LlmAgent that calls the generate_hero_image tool exactly once."""
    return LlmAgent(
        name=f"ImageGenLLM_{product_id}",
        model=make_llm(),
        instruction=(
            "You are responsible for generating a single hero image for a marketing "
            "campaign. The user message will contain the prompt and the output path. "
            "Call the generate_hero_image tool exactly once with: prompt=<the prompt>, "
            "out_path=<the out_path>, aspect_ratio='1:1'. Do not call any other tool. "
            "After the tool returns, reply with a one-sentence confirmation."
        ),
        tools=[generate_hero_image],
        before_model_callback=legal_precheck_callback,
    )


class ImageGeneratorAgent(BaseAgent):
    """Per-product custom agent. Cache check + LiteLLM-backed tool invocation."""

    product_id: str = ""

    async def _run_async_impl(self, ctx):
        state = ctx.session.state
        product_key = f"product:{self.product_id}"
        product_state = dict(state.get(product_key, {}))

        # Cache hit — no LLM call, no image-gen API call.
        if product_state.get("hero_path"):
            text = f"[cache hit] reusing hero for {self.product_id}: {product_state['hero_path']}"
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
        out_path = f"inputs/assets/{self.product_id}/hero.png"
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)

        inner_agent = _build_inner_llm_agent(self.product_id)
        runner = InMemoryRunner(
            agent=inner_agent, app_name=f"image_gen_{self.product_id}"
        )
        sub_session = await runner.session_service.create_session(
            app_name=f"image_gen_{self.product_id}",
            user_id="pipeline",
            # Forward brand/brief into the sub-session so legal_precheck_callback
            # can read state["brief"] / state["brand"] from the inner LlmAgent.
            state={"brief": state["brief"], "brand": state["brand"]},
        )

        user_msg = types.Content(
            role="user",
            parts=[types.Part(text=(
                f"prompt: {prompt}\n"
                f"out_path: {out_path}\n"
                f"aspect_ratio: 1:1"
            ))],
        )

        tool_result: dict | None = None
        async for inner_event in runner.run_async(
            user_id="pipeline", session_id=sub_session.id, new_message=user_msg
        ):
            # Capture the function-response payload from the tool the LLM called.
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

        product_state["hero_path"] = tool_result["path"]
        product_state["asset_source"] = "generated"
        product_state["image_model"] = tool_result["model"]
        product_state["image_gen_latency_ms"] = tool_result["latency_ms"]

        text = (
            f"Generated hero for {self.product_id}: {tool_result['path']} "
            f"({tool_result['model']}, {tool_result['latency_ms']}ms)"
        )
        logger.info(text)
        yield Event(
            author=self.name,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            actions=EventActions(state_delta={product_key: product_state}),
        )


image_generator_agent = ImageGeneratorAgent(name="ImageGeneratorAgent")
