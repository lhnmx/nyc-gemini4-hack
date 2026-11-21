from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from nyc_gemini4_hack.agents.planner import synthesize_build_plan
from nyc_gemini4_hack.agents.researcher import research_goal_on_youtube
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import ProjectState, ProjectStatus, StepMediaAsset

logger = logging.getLogger(__name__)

EventPublisher = Callable[[str, str, dict[str, object]], Awaitable[None]]


async def run_project_pipeline(
    project: ProjectState,
    publish_event: EventPublisher | None = None,
    direct_urls: list[str] | None = None,
    max_videos: int = 2,
) -> ProjectState:
    """
    End-to-end async orchestrator.
    Currently wired through Slice 2:
      - Research Agent: YouTube Data API v3 + Decisions API + Gemini 4 Carbon
      - Planner Agent: Gemini 4 Argon + Gemini 4 Hydrogen (Physical State Graph)
    Slices 3 & 4 will plug in Shopify MCP Commerce + POV Media Generation here.
    """
    gemini = Gemini4Client()

    async def emit(event: str, data: dict[str, object]) -> None:
        if publish_event is not None:
            await publish_event(project.project_id, event, data)

    try:
        # 1. YouTube Research (Carbon + Decisions API)
        project.status = ProjectStatus.RESEARCHING
        project.status_message = (
            "Gemini 4 Carbon & Decisions API watching top YouTube tutorials…"
        )
        await emit(
            "status",
            {"status": project.status.value, "message": project.status_message},
        )

        extracts, decisions = await research_goal_on_youtube(
            goal=project.goal,
            gemini=gemini,
            max_videos=max_videos,
            direct_urls=direct_urls,
        )
        project.decisions_log.extend(decisions)

        await emit(
            "research_complete",
            {
                "videos_analyzed": [
                    {"title": e.title, "url": e.url, "audio": e.language_or_audio_notes}
                    for e in extracts
                ],
                "decisions": [d.model_dump() for d in decisions],
            },
        )

        # 2. Physical Planning & World-State Grounding (Argon + Hydrogen)
        project.status = ProjectStatus.PLANNING
        project.status_message = (
            "Gemini 4 Argon & Hydrogen synthesizing Physical State Graph & BOM…"
        )
        await emit(
            "status",
            {"status": project.status.value, "message": project.status_message},
        )

        plan = await synthesize_build_plan(
            goal=project.goal,
            extracts=extracts,
            owned_items=project.owned_items,
            gemini=gemini,
        )
        project.plan = plan
        project.media = {
            step.step_number: StepMediaAsset(step_number=step.step_number, status="pending")
            for step in plan.steps
        }

        await emit("plan_ready", plan.model_dump())

        # Slice 3 (Shopify MCP) & Slice 4 (POV Media) hook in here next
        project.status = ProjectStatus.COMPLETED
        project.status_message = (
            f"Build plan ready ({len(plan.steps)} steps, {len(plan.materials)} materials) "
            f"from {len(extracts)} YouTube tutorial(s)."
        )
        await emit(
            "status",
            {"status": project.status.value, "message": project.status_message},
        )
        return project

    except Exception as exc:
        logger.exception("Pipeline failed for project %s", project.project_id)
        project.status = ProjectStatus.FAILED
        project.error = str(exc)
        project.status_message = f"Error: {exc}"
        await emit(
            "error",
            {"status": project.status.value, "message": project.status_message},
        )
        return project
