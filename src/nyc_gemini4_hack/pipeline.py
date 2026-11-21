from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from nyc_gemini4_hack.agents.planner import synthesize_build_plan
from nyc_gemini4_hack.agents.researcher import research_goal_on_youtube
from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import compose_step_pov_video, stitch_full_tutorial
from nyc_gemini4_hack.media.video_gen import generate_step_pov_frames
from nyc_gemini4_hack.media.voice_gen import generate_step_voiceover
from nyc_gemini4_hack.models import ProjectState, ProjectStatus, StepMediaAsset

logger = logging.getLogger(__name__)

EventPublisher = Callable[[str, str, dict[str, object]], Awaitable[None]]


async def generate_media_for_plan(
    project: ProjectState,
    gemini: Gemini4Client,
    publish_event: EventPublisher | None = None,
    max_steps: int | None = None,
    prefer_gemini_audio: bool = False,
) -> Path | None:
    """
    Generate continuity-chained POV videos + synced voiceovers for each step in project.plan,
    then stitch them into a single full_pov_tutorial.mp4.
    """
    if project.plan is None:
        raise ValueError("Cannot generate media without a BuildPlan.")

    async def emit(event: str, data: dict[str, object]) -> None:
        if publish_event is not None:
            await publish_event(project.project_id, event, data)

    settings = get_settings()
    project_dir = settings.ensure_output_dir() / project.project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    steps = project.plan.steps[:max_steps] if max_steps else project.plan.steps
    project.status = ProjectStatus.GENERATING
    project.status_message = f"Generating POV video + voice for {len(steps)} steps…"
    await emit("status", {"status": project.status.value, "message": project.status_message})

    prev_end_frame: Path | None = None
    produced_mp4s: list[Path] = []

    for step in steps:
        asset = project.media.setdefault(
            step.step_number,
            StepMediaAsset(step_number=step.step_number, status="generating"),
        )
        asset.status = "generating"
        await emit(
            "status",
            {
                "status": project.status.value,
                "message": f"Rendering Step {step.step_number}/{len(steps)}: {step.title}…",
            },
        )

        try:
            voice_wav = project_dir / f"step_{step.step_number:02d}_voice.wav"
            step_mp4 = project_dir / f"step_{step.step_number:02d}_pov.mp4"

            # Run POV frame generation + voiceover synthesis concurrently for this step
            frames_task = generate_step_pov_frames(
                step=step,
                bible=project.plan.continuity_bible,
                out_dir=project_dir,
                gemini=gemini,
                prev_step_end_frame=prev_end_frame,
            )
            voice_task = generate_step_voiceover(
                script=step.narration_script,
                out_wav=voice_wav,
                gemini=gemini,
                prefer_gemini_audio=prefer_gemini_audio,
            )
            frames, wav_path = await asyncio.gather(frames_task, voice_task)

            await compose_step_pov_video(
                frames=frames,
                voice_wav=wav_path,
                out_mp4=step_mp4,
            )

            prev_end_frame = frames[-1]
            produced_mp4s.append(step_mp4)

            asset.status = "ready"
            asset.keyframe_url = f"/media/{project.project_id}/{frames[1].name}"
            asset.audio_url = f"/media/{project.project_id}/{wav_path.name}"
            asset.video_url = f"/media/{project.project_id}/{step_mp4.name}"
            await emit("step_media_ready", asset.model_dump())

        except Exception as exc:
            logger.exception("Failed to generate media for step %s", step.step_number)
            asset.status = "error"
            asset.error = str(exc)
            await emit("step_media_error", asset.model_dump())

    full_mp4: Path | None = None
    if produced_mp4s:
        full_mp4 = project_dir / "full_pov_tutorial.mp4"
        await stitch_full_tutorial(produced_mp4s, full_mp4)
        await emit(
            "full_video_ready",
            {"video_url": f"/media/{project.project_id}/{full_mp4.name}"},
        )

    return full_mp4


async def run_project_pipeline(
    project: ProjectState,
    publish_event: EventPublisher | None = None,
    direct_urls: list[str] | None = None,
    max_videos: int = 2,
    generate_media: bool = True,
    max_media_steps: int | None = None,
) -> ProjectState:
    """
    End-to-end async orchestrator:
      1. Research Agent: YouTube Data API v3 + Decisions API + Gemini 4 Carbon
      2. Planner Agent: Gemini 4 Argon + Gemini 4 Hydrogen (Physical State Graph)
      3. POV Media Pipeline: Continuity-chained POV frames + Voiceover + FFmpeg MP4
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

        # 3. POV Video + Voice Generation
        if generate_media:
            await generate_media_for_plan(
                project=project,
                gemini=gemini,
                publish_event=publish_event,
                max_steps=max_media_steps,
            )

        project.status = ProjectStatus.COMPLETED
        ready_count = sum(1 for m in project.media.values() if m.status == "ready")
        project.status_message = (
            f"Completed: {len(plan.steps)} steps planned, {ready_count} POV video(s) rendered."
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
