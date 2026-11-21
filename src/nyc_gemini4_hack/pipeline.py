from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from nyc_gemini4_hack.agents.commerce import build_shopping_cart_proposal
from nyc_gemini4_hack.agents.planner import synthesize_build_plan
from nyc_gemini4_hack.agents.researcher import research_goal_on_youtube
from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import (
    NarratedChapter, caption_chapter, compose_step_pov_video, stitch_full_tutorial,
    write_tutorial_captions,
)
from nyc_gemini4_hack.media.motion_gen import compose_motion_with_voice, generate_step_motion
from nyc_gemini4_hack.media.video_gen import generate_step_pov_frames
from nyc_gemini4_hack.media.voice_gen import generate_step_voiceover
from nyc_gemini4_hack.models import BuildStep, ProjectState, ProjectStatus, StepMediaAsset

logger = logging.getLogger(__name__)

EventPublisher = Callable[[str, str, dict[str, object]], Awaitable[None]]


async def generate_media_for_plan(
    project: ProjectState,
    gemini: Gemini4Client,
    publish_event: EventPublisher | None = None,
    max_steps: int | None = None,
    prefer_gemini_audio: bool = True,
) -> Path | None:
    """
    Generate continuity-chained POV videos + synced voiceovers for each step in project.plan,
    then stitch them into a single full_pov_tutorial.mp4.
    """
    if project.plan is None:
        raise ValueError("Cannot generate media without a BuildPlan.")
    if max_steps is not None and max_steps < 1:
        raise ValueError("The number of steps to render must be positive.")

    async def emit(event: str, data: dict[str, object]) -> None:
        if publish_event is not None:
            await publish_event(project.project_id, event, data)

    settings = get_settings()
    project_dir = settings.ensure_output_dir() / project.project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    media_base = f"/media/{project.project_id}/"
    mode = settings.pov_media_mode
    generate_visuals = generate_step_motion if mode == "motion" else generate_step_pov_frames

    steps = project.plan.steps[:max_steps]
    project.full_video_url = None
    project.full_captions_url = None
    project.error = None
    for existing in project.media.values():
        existing.chapter_start_seconds = None
    # Before motion generation existed, saved chapters were all illustrated.
    reusable: list[tuple[BuildStep, StepMediaAsset, Path]] = []
    for step in project.plan.steps[len(steps):]:
        asset = project.media.get(step.step_number)
        path = project_dir / f"step_{step.step_number:02d}_pov.mp4"
        if (
            asset and asset.status == "ready" and path.is_file()
            and asset.video_url == media_base + path.name
            and (asset.media_mode or "illustrated_frames") == mode
        ):
            reusable.append((step, asset, path))
        else:
            project.media[step.step_number] = StepMediaAsset(step_number=step.step_number, status="skipped")
    project.status = ProjectStatus.GENERATING
    project.status_message = f"Preparing the film and narration for {len(steps)} steps."
    await emit("status", {"status": project.status.value, "message": project.status_message})

    prev_end_frame: Path | None = None
    chapters: list[NarratedChapter] = []

    for position, step in enumerate(steps, start=1):
        asset = StepMediaAsset(step_number=step.step_number, status="generating", media_mode=mode)
        project.media[step.step_number] = asset
        project.status_message = f"Rendering step {position} of {len(project.plan.steps)} · {step.title}"
        await emit(
            "status",
            {
                "status": project.status.value,
                "message": project.status_message,
                "step_number": step.step_number,
            },
        )

        stage = "motion footage" if mode == "motion" else "illustrations"
        try:
            voice_wav = project_dir / f"step_{step.step_number:02d}_voice.wav"
            step_mp4 = project_dir / f"step_{step.step_number:02d}_pov.mp4"

            visuals_task = generate_visuals(
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
                duration_seconds=step.duration_seconds,
            )
            # Let both producers finish before touching the next step's files or reference.
            visuals, wav_path = await asyncio.gather(visuals_task, voice_task, return_exceptions=True)
            prev_end_frame = None
            if isinstance(visuals, BaseException):
                raise visuals
            stage = "narration"
            if isinstance(wav_path, BaseException):
                raise wav_path

            stage = "video assembly"
            if mode == "motion":
                step_mp4, poster, end_frame = await compose_motion_with_voice(
                    video=visuals, voice_wav=wav_path, out_mp4=step_mp4,
                )
            else:
                await compose_step_pov_video(
                    frames=visuals, voice_wav=wav_path, out_mp4=step_mp4, min_duration=step.duration_seconds,
                )
                poster, end_frame = visuals[0], visuals[-1]
            stage = "captions"
            chapter = await asyncio.to_thread(caption_chapter, step_mp4, wav_path, step.narration_script)
            prev_end_frame = end_frame
            chapters.append(chapter)

            asset.status = "ready"
            asset.keyframe_url = media_base + poster.name
            asset.audio_url = media_base + wav_path.name
            asset.video_url = media_base + step_mp4.name
            asset.captions_url = media_base + step_mp4.with_suffix(".vtt").name
            asset.duration_seconds = chapter.duration_seconds
        except Exception:
            logger.exception("Failed to generate media for step %s", step.step_number)
            asset.status = "error"
            asset.error = f"Step {step.step_number} {stage} could not be generated."
            await emit("step_media_error", asset.model_dump())
        else:
            await emit("step_media_ready", asset.model_dump())

    full_mp4: Path | None = None
    ready, requested = len(chapters), len(steps)
    if ready + len(reusable) == len(project.plan.steps):
        try:
            for step, asset, video in reusable:
                wav = project_dir / f"step_{step.step_number:02d}_voice.wav"
                recorded_voice = wav if wav.is_file() else None
                chapter = await asyncio.to_thread(
                    caption_chapter, video, recorded_voice, step.narration_script,
                )
                if recorded_voice is None and asset.audio_url == media_base + wav.name:
                    asset.audio_url = None
                asset.media_mode = mode
                asset.duration_seconds = chapter.duration_seconds
                asset.captions_url = media_base + video.with_suffix(".vtt").name
                chapters.append(chapter)
            video = await stitch_full_tutorial(
                [chapter.video for chapter in chapters], project_dir / "full_pov_tutorial.mp4",
                durations=[chapter.duration_seconds for chapter in chapters],
            )
            captions = video.with_suffix(".vtt")
            starts = await asyncio.to_thread(write_tutorial_captions, chapters, captions)
        except Exception:
            logger.exception("Could not stitch tutorial for %s", project.project_id)
            project.error = "The individual clips are ready. The full video could not be assembled."
        else:
            full_mp4 = video
            project.full_video_url = media_base + video.name
            project.full_captions_url = media_base + captions.name
            for step, start in zip(project.plan.steps, starts, strict=True):
                project.media[step.step_number].chapter_start_seconds = start
            await emit("full_video_ready", {
                "video_url": project.full_video_url,
                "captions_url": project.full_captions_url,
                "media": {str(number): asset.model_dump() for number, asset in project.media.items()},
            })

    if ready < requested:
        project.status = ProjectStatus.PARTIAL if ready else ProjectStatus.FAILED
        project.error = f"{ready} of {requested} requested clips are ready. The written guide is available."
    elif project.error:
        project.status = ProjectStatus.PARTIAL
    else:
        project.status = ProjectStatus.COMPLETED
    if project.error:
        project.status_message = project.error
    elif requested < len(project.plan.steps):
        available = sum(asset.status == "ready" for asset in project.media.values())
        project.status_message = (
            f"Rendered {ready} of {requested} requested clips. "
            f"{available} of {len(project.plan.steps)} guide steps have video."
        )
    else:
        project.status_message = f"Your narrated film is ready with {ready} chapters."
    await emit("status", {"status": project.status.value, "message": project.status_message})

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
      3. POV Media Pipeline: Continuity-chained action shots, narration and a chaptered film.
    """
    async def emit(event: str, data: dict[str, object]) -> None:
        if publish_event is not None:
            await publish_event(project.project_id, event, data)

    gemini: Gemini4Client | None = None
    try:
        gemini = Gemini4Client()
        # 1. YouTube Research (Carbon + Decisions API)
        project.status = ProjectStatus.RESEARCHING
        project.status_message = "Finding and reviewing hands-on tutorials."
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
        project.status_message = "Organizing the materials and working out each step."
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
        project.full_video_url = None
        project.full_captions_url = None
        project.error = None
        initial_media_status = "pending" if generate_media else "skipped"
        project.media = {
            step.step_number: StepMediaAsset(step_number=step.step_number, status=initial_media_status)
            for step in plan.steps
        }

        await emit("plan_ready", plan.model_dump())

        # 3. Run Shopify MCP Commerce (Global Catalog options + Mock Store cart)
        #    in parallel with POV Video + Voice Generation so cart_ready lands early.
        async def _run_commerce() -> None:
            try:
                cart = await build_shopping_cart_proposal(materials=plan.materials)
                project.cart = cart
                await emit("cart_ready", cart.model_dump(mode="json"))
            except Exception as exc:
                logger.warning("Shopify MCP commerce step failed: %s", exc)

        commerce_task = asyncio.create_task(_run_commerce()) if plan.materials else None

        if generate_media:
            await generate_media_for_plan(
                project=project,
                gemini=gemini,
                publish_event=publish_event,
                max_steps=max_media_steps,
            )
            if commerce_task is not None:
                await commerce_task
        else:
            if commerce_task is not None:
                await commerce_task
            project.status = ProjectStatus.COMPLETED
            project.status_message = f"Your written guide is ready with {len(plan.steps)} steps."
            await emit("status", {"status": project.status.value, "message": project.status_message})
        return project

    except Exception:
        logger.exception("Pipeline failed for project %s", project.project_id)
        failed_stage = project.status
        project.status = ProjectStatus.FAILED
        project.error = {
            ProjectStatus.RESEARCHING: "Tutorial research failed. Check the YouTube and Gemini configuration and try again.",
            ProjectStatus.PLANNING: "The build plan could not be prepared. Try creating the guide again.",
            ProjectStatus.GENERATING: "The clips could not be prepared. The written guide is available.",
        }.get(failed_stage, "The project could not be started. Check the server configuration.")
        project.status_message = project.error
        await emit(
            "error",
            {"status": project.status.value, "message": project.status_message},
        )
        await emit("status", {"status": project.status.value, "message": project.status_message})
        return project
    finally:
        if gemini is not None:
            await gemini.aclose()
