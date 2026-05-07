"""One-shot driver to run the full root_agent pipeline non-interactively.

Used by reviewers / CI to trigger an end-to-end render without spinning up
`adk web`. Reads the standard env (PROVIDER, MODEL, IMAGE_PROVIDER, etc.)
and prints the report path on completion.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from google.adk.runners import InMemoryRunner
from google.genai import types

from creative_pipeline.agent import root_agent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> int:
    runner = InMemoryRunner(agent=root_agent, app_name="creative_pipeline_oneshot")
    user_id = "cli"
    session = await runner.session_service.create_session(
        app_name="creative_pipeline_oneshot", user_id=user_id
    )
    msg = types.Content(role="user", parts=[types.Part(text="run")])
    async for event in runner.run_async(user_id=user_id, session_id=session.id, new_message=msg):
        if event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    logger.info("[%s] %s", event.author or "agent", part.text)
    final = await runner.session_service.get_session(
        app_name="creative_pipeline_oneshot", user_id=user_id, session_id=session.id
    )
    report_path = final.state.get("report_path")
    if report_path:
        print(f"REPORT: {report_path}")
        return 0
    print("ERROR: pipeline finished without a report_path", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
