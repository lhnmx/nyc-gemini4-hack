from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

from nyc_gemini4_hack.agents.commerce import build_shopping_cart_proposal
from nyc_gemini4_hack.agents.planner import synthesize_build_plan
from nyc_gemini4_hack.agents.researcher import research_goal_on_youtube
from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import (
    NarratedChapter, caption_chapter, compose_step_pov_video, stitch_full_tutorial,
    _wav_duration_seconds, run_media_work, write_tutorial_captions,
)
from nyc_gemini4_hack.media.motion_gen import (
    MotionOutputError, MotionShot, compose_motion_with_voice, generate_motion_shot,
)
from nyc_gemini4_hack.media.shot_review import inspect_illustrations
from nyc_gemini4_hack.media.video_gen import generate_step_pov_frames
from nyc_gemini4_hack.media.voice_gen import generate_step_voiceover
from nyc_gemini4_hack.models import (
    AgentActivity, BuildStep, DecisionResult, MediaProgress, ProjectState, ProjectStatus, StepMediaAsset, VisualReview,
)

logger = logging.getLogger(__name__)

EventPublisher = Callable[[str, str, dict[str, object]], Awaitable[None]]
ILLUSTRATION_TIMEOUT = 180


def _review_reason(review: VisualReview) -> str:
    for finding in (review.result, review.contact, review.opening):
        if finding and finding.verdict == "contradicts":
            return " ".join(finding.observation.split())
    return {
        "rejected": "The images contradicted the required technique.",
        "uncertain": "The visual review was inconclusive.",
        "unavailable": " ".join(filter(None, ("The visual review was unavailable.", review.unavailable_reason))),
    }.get(review.status, "")


def _generation_failure(label: str, error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return f"{label} timed out."
    if isinstance(error, MotionOutputError):
        return str(error)
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(code, int) and 400 <= code <= 599:
        return f"{label} failed (provider returned HTTP {code})."
    return f"{label} could not be generated."


async def generate_media_for_plan(
    project: ProjectState,
    gemini: Gemini4Client,
    publish_event: EventPublisher | None = None,
    max_steps: int | None = None,
    prefer_gemini_audio: bool = True,
    *,
    before_completion: Awaitable[None] | None = None,
) -> Path | None:
    """Render each physical sequence from its parent frames and assemble the film in guide order."""
    if project.plan is None:
        raise ValueError("Cannot generate media without a BuildPlan.")
    if max_steps is not None and max_steps < 1:
        raise ValueError("The number of steps to render must be positive.")

    async def emit(event: str, data: dict[str, object]) -> None:
        if publish_event is not None:
            await publish_event(project.project_id, event, data)

    settings = gemini.settings
    project_dir = settings.ensure_output_dir() / project.project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    media_base = f"/media/{project.project_id}/"
    mode = settings.pov_media_mode

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
            and ((asset.media_mode or "illustrated_frames") == mode or (
                mode == "motion" and asset.media_mode == "illustrated_frames" and asset.motion_failure
            ))
        ):
            reusable.append((step, asset, path))
        else:
            project.media[step.step_number] = StepMediaAsset(step_number=step.step_number, status="skipped")
    project.status = ProjectStatus.GENERATING
    project.status_message = "Preparing chapter footage and narration."
    started = monotonic()
    chapters: dict[int, NarratedChapter] = {}
    for step in steps:
        project.media[step.step_number] = StepMediaAsset(step_number=step.step_number, media_mode=mode)

    async def progress(stage, message: str, step: BuildStep | None = None) -> None:
        project.status_message = message
        project.media_progress = MediaProgress(
            stage=stage, step_number=step.step_number if step else None, step_count=len(steps),
            ready_count=len(chapters), elapsed_seconds=round(monotonic() - started, 3),
        )
        data = {"status": project.status.value, "message": message,
                "media_progress": project.media_progress.model_dump()}
        if stage == "footage" and step:
            data["step_number"] = step.step_number
        await emit("status", data)

    async def outcome[T](operation: Callable[..., Awaitable[T]], *args, **kwargs) -> T | Exception:
        # A single provider failure is a chapter error. Cancellation still stops the group.
        try:
            return await operation(*args, **kwargs)
        except Exception as exc:
            return exc

    voice_slots = asyncio.Semaphore(2)
    shot_slots = asyncio.Semaphore(settings.max_parallel_shots)
    assembly_slots = asyncio.Semaphore(2)
    visual_model = gemini.settings.pov_video_model if mode == "motion" else gemini.settings.pov_image_model
    media_stages = {
        "motion": ("Chapter footage" if mode == "motion" else "Chapter illustrations", "Gemini"),
        "illustration": ("Chapter illustrations", "Gemini"),
        "narration": ("Chapter narration", "Speech generator"),
        "assembly": ("Chapter encoding and captions", "FFmpeg"),
    }
    retained_steps = {step.step_number for step, _, _ in reusable}
    project.activity = [entry for entry in project.activity
                        if entry.stage not in media_stages or entry.step_number in retained_steps]

    async def activity(stage, state, step: BuildStep | None = None, model: str | None = None,
                       detail: str = "") -> None:
        label, agent = media_stages[stage]
        number = step.step_number if step else None
        entry = AgentActivity(id=f"{stage}-{number or 'film'}", stage=stage, state=state, agent=agent,
                              label=label if step else "Full film assembly", step_number=number, model=model,
                              detail=detail)
        project.record_activity(entry)
        await emit("activity", entry.model_dump())

    async def voice(step: BuildStep) -> tuple[Path, str | None] | Exception:
        model_used = None

        def record_model(name: str) -> None:
            nonlocal model_used
            model_used = name

        async with voice_slots:
            await activity("narration", "started", step)
            wav = await outcome(
                generate_step_voiceover,
                script=step.narration_script, out_wav=project_dir / f"step_{step.step_number:02d}_voice.wav",
                gemini=gemini, prefer_gemini_audio=prefer_gemini_audio, duration_seconds=step.duration_seconds,
                on_voice_model=record_model,
            )
            if isinstance(wav, Exception):
                await activity("narration", "failed", step)
                return wav
            asset = project.media[step.step_number]
            asset.audio_url = media_base + wav.name
            asset.voice_model = model_used
            await activity("narration", "completed", step, model_used)
            return wav, model_used

    async def visuals(step: BuildStep, previous: Path | None, recording: asyncio.Task) -> MotionShot | list[Path]:
        arguments = dict(step=step, bible=project.plan.continuity_bible, out_dir=project_dir,
                         gemini=gemini, prev_step_end_frame=previous)
        if mode == "illustrated_frames":
            return await generate_step_pov_frames(**arguments)
        result = recording.result() if recording.done() else None
        seconds = _wav_duration_seconds(result[0]) if isinstance(result, tuple) else None
        return await generate_motion_shot(**arguments, speech_seconds=seconds)

    async def film(
        index: int, step: BuildStep, parent: asyncio.Task | None, recording: asyncio.Task,
        after: asyncio.Task | None,
    ) -> MotionShot | list[Path] | Exception:
        # In serial mode finish each visual job in guide order, including review and recovery.
        # The continuity frame still comes only from the step's declared physical parent.
        if after is not None:
            await after
        source = await parent if parent is not None else None
        reference = source.continuity_frame if isinstance(source, MotionShot) else None
        if isinstance(source, list) and source:
            parent_number = steps[index - 1].step_number if step.visual_parent == -1 else step.visual_parent
            review = project.media[parent_number].illustration_review
            if mode == "illustrated_frames" or (review and review.status == "passed"):
                reference = source[-1]
        async with shot_slots:
            asset = project.media[step.step_number]
            asset.status = "generating"
            footage = group.create_task(outcome(visuals, step, reference, recording))
            await progress("footage", f"Rendering step {index + 1} of {len(steps)} · {step.title}", step)
            await emit("step_media_started", asset.model_dump())
            await activity("motion", "started", step, visual_model)
            shot = await footage
            if mode == "motion" and isinstance(shot, Exception):
                asset.video_model = visual_model
                asset.motion_failure = _generation_failure("Motion footage", shot)
                await activity("motion", "failed", step, visual_model, asset.motion_failure)
                return await recover_illustrations(step, asset, reference)
            return shot

    async def recover_illustrations(
        step: BuildStep, asset: StepMediaAsset, reference: Path | None,
    ) -> list[Path] | Exception:
        model = gemini.settings.pov_image_model
        await activity("illustration", "started", step, model, "Preparing setup, action and result illustrations.")
        try:
            with TemporaryDirectory(dir=project_dir) as staging:
                async with asyncio.timeout(ILLUSTRATION_TIMEOUT):
                    frames = await generate_step_pov_frames(
                        step=step, bible=project.plan.continuity_bible, out_dir=Path(staging),
                        gemini=gemini, prev_step_end_frame=reference,
                    )
                if len(frames) != 3:
                    raise RuntimeError("The illustration sequence was incomplete.")
                try:
                    asset.illustration_review = await inspect_illustrations(gemini, step, frames)
                except Exception as exc:
                    logger.info("Step %s illustration review unavailable (%s)", step.step_number, type(exc).__name__)
                finished_frames = [frame.replace(project_dir / frame.name) for frame in frames]
        except Exception as exc:
            logger.warning("Step %s illustration recovery failed (%s)", step.step_number, type(exc).__name__)
            reason = _generation_failure("Chapter illustrations", exc)
        else:
            asset.media_mode, asset.video_model = "illustrated_frames", model
            review = asset.illustration_review
            detail = _review_reason(review) if review and review.status != "passed" else "Illustrated sequence generated."
            await activity("illustration", "completed", step, model, detail)
            return finished_frames
        await activity("illustration", "failed", step, model, reason)
        asset.error = f"{asset.motion_failure} {reason}"
        return RuntimeError(reason)

    async def fail_step(asset: StepMediaAsset, stage: str, error: Exception) -> None:
        logger.error("Failed to generate %s for step %s", stage, asset.step_number, exc_info=error)
        asset.status = "error"
        asset.error = asset.error or _generation_failure(f"Step {asset.step_number} {stage}", error)
        await emit("step_media_error", asset.model_dump())

    async def finish(index: int, step: BuildStep, footage: asyncio.Task, recording: asyncio.Task) -> None:
        number = step.step_number
        asset = project.media[number]
        shot = await footage
        if isinstance(shot, Exception):
            if not asset.motion_failure:
                await activity("motion", "failed", step, visual_model, _generation_failure("Chapter illustrations", shot))
            await recording  # The error event also carries any independently completed narration.
            await fail_step(asset, "motion footage" if mode == "motion" else "illustrations", shot)
            return
        detail = ""
        if isinstance(shot, MotionShot):
            asset.shot_reviews = list(shot.reviews)
            asset.selected_motion_attempt = shot.selected_attempt
            review = shot.selected_review
            if review:
                detail = (f"Visual review passed on take {review.attempt} across {len(review.sampled_seconds)} frames."
                          if review.status == "passed" else _review_reason(review))
        if not asset.motion_failure:
            asset.video_model = visual_model
            await activity("motion", "completed", step, visual_model, detail)
        audio = await recording
        if isinstance(audio, Exception):
            await fail_step(asset, "narration", audio)
            return
        wav, asset.voice_model = audio

        async with assembly_slots:
            await progress("assembly", f"Adding narration and captions to step {index + 1} of {len(steps)}.", step)
            await activity("assembly", "started", step)
            stage = "video assembly"
            step_mp4 = project_dir / f"step_{number:02d}_pov.mp4"
            try:
                if isinstance(shot, MotionShot):
                    step_mp4, poster, _ = await compose_motion_with_voice(video=shot, voice_wav=wav, out_mp4=step_mp4)
                else:
                    await compose_step_pov_video(
                        frames=shot, voice_wav=wav, out_mp4=step_mp4, min_duration=step.duration_seconds,
                    )
                    poster = shot[1] if asset.motion_failure else shot[0]
                stage = "captions"
                chapter = await run_media_work(caption_chapter, step_mp4, wav, step.narration_script)
            except Exception as exc:
                await fail_step(asset, stage, exc)
                await activity("assembly", "failed", step)
                return

            chapters[number] = chapter
            asset.status = "ready"
            asset.keyframe_url = media_base + poster.name
            asset.audio_url = media_base + wav.name
            asset.video_url = media_base + step_mp4.name
            asset.captions_url = media_base + step_mp4.with_suffix(".vtt").name
            asset.duration_seconds = chapter.duration_seconds
            project.media_progress.ready_count = len(chapters)
            project.media_progress.elapsed_seconds = round(monotonic() - started, 3)
            await emit("step_media_ready", asset.model_dump())
            await activity("assembly", "completed", step)

    async with asyncio.TaskGroup() as group:
        # Nodes share only the footage task of their *declared* parent. A failed parent
        # yields no reference; its children can restart from their grounded opening state.
        footage_by_step = {0: None}
        finishes = []
        previous = None
        for index, step in enumerate(steps):
            parent = previous if step.visual_parent == -1 else footage_by_step[step.visual_parent]
            recording = group.create_task(voice(step))
            after = previous if settings.max_parallel_shots == 1 else None
            footage = group.create_task(film(index, step, parent, recording, after))
            finishes.append(group.create_task(finish(index, step, footage, recording)))
            footage_by_step[step.step_number] = footage
            previous = footage
        # Propagate a cancelled provider or publisher to the whole project as well as external cancellation.
        await asyncio.gather(*finishes)

    full_mp4: Path | None = None
    ready, requested = len(chapters), len(steps)
    if ready + len(reusable) == len(project.plan.steps):
        await progress("stitching", "Assembling the full film and aligning chapter captions.")
        await activity("assembly", "started")
        try:
            for step, asset, video in reusable:
                wav = project_dir / f"step_{step.step_number:02d}_voice.wav"
                recorded_voice = wav if wav.is_file() else None
                chapter = await run_media_work(
                    caption_chapter, video, recorded_voice, step.narration_script,
                )
                if recorded_voice is None and asset.audio_url == media_base + wav.name:
                    asset.audio_url = None
                asset.media_mode = asset.media_mode or "illustrated_frames"
                asset.duration_seconds = chapter.duration_seconds
                asset.captions_url = media_base + video.with_suffix(".vtt").name
                chapters[step.step_number] = chapter
            ordered_chapters = [chapters[step.step_number] for step in project.plan.steps]
            video = await stitch_full_tutorial(
                [chapter.video for chapter in ordered_chapters], project_dir / "full_pov_tutorial.mp4",
                durations=[chapter.duration_seconds for chapter in ordered_chapters],
            )
            captions = video.with_suffix(".vtt")
            starts = await run_media_work(write_tutorial_captions, ordered_chapters, captions)
        except Exception:
            logger.exception("Could not stitch tutorial for %s", project.project_id)
            project.error = "The individual clips are ready. The full video could not be assembled."
            await activity("assembly", "failed")
        else:
            full_mp4 = video
            project.full_video_url = media_base + video.name
            project.full_captions_url = media_base + captions.name
            for step, start in zip(project.plan.steps, starts, strict=True):
                project.media[step.step_number].chapter_start_seconds = start
            await activity("assembly", "completed")
            await emit("full_video_ready", {
                "video_url": project.full_video_url,
                "captions_url": project.full_captions_url,
                "media": {str(number): asset.model_dump() for number, asset in project.media.items()},
            })

    project.media_progress.elapsed_seconds = round(monotonic() - started, 3)
    # A terminal status ends the client's event stream and releases the worker lease.
    if before_completion is not None:
        await before_completion
    if ready < requested:
        project.status = ProjectStatus.PARTIAL if ready or reusable else ProjectStatus.FAILED
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
    await emit("status", {
        "status": project.status.value, "message": project.status_message,
        "media_progress": project.media_progress.model_dump(),
    })

    return full_mp4


async def run_project_pipeline(
    project: ProjectState,
    publish_event: EventPublisher | None = None,
    direct_urls: list[str] | None = None,
    max_videos: int = 1,
    generate_media: bool = True,
    max_media_steps: int | None = None,
    *,
    settings: Settings | None = None,
    planning_slot: asyncio.Lock | None = None,
    media_slot: asyncio.Lock | None = None,
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

    async def record_activity(entry: AgentActivity) -> None:
        project.record_activity(entry)
        await emit("activity", entry.model_dump())

    async def record_decisions(decisions: list[DecisionResult]) -> None:
        project.decisions_log.extend(decisions)
        await emit("research_decisions", {"decisions": [decision.model_dump() for decision in project.decisions_log]})

    gemini: Gemini4Client | None = None
    try:
        settings = settings or get_settings()
        gemini = Gemini4Client(settings)
        async with planning_slot or nullcontext():
            project.status = ProjectStatus.RESEARCHING
            project.status_message = "Finding and reviewing hands-on tutorials."
            await emit(
                "status",
                {"status": project.status.value, "message": project.status_message},
            )

            extracts, _ = await research_goal_on_youtube(
                goal=project.goal,
                gemini=gemini,
                max_videos=max_videos,
                direct_urls=direct_urls,
                on_activity=record_activity,
                on_decisions=record_decisions,
            )

            await emit(
                "research_complete",
                {
                    "videos_analyzed": [
                        {"title": e.title, "url": e.url, "audio": e.language_or_audio_notes}
                        for e in extracts
                    ],
                    "decisions": [d.model_dump() for d in project.decisions_log],
                },
            )

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
                on_activity=record_activity,
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

        async def prepare_cart() -> None:
            try:
                cart = await build_shopping_cart_proposal(materials=plan.materials, settings=settings)
            except Exception as exc:
                logger.warning("Shopify MCP commerce step failed: %s", exc)
                return
            project.cart = cart
            await emit("cart_ready", cart.model_dump(mode="json"))

        async with asyncio.TaskGroup() as tasks:
            commerce = tasks.create_task(prepare_cart()) if plan.materials else None
            if generate_media:
                if media_slot and media_slot.locked():
                    project.status = ProjectStatus.GENERATING
                    project.status_message = "Read the steps or shop for materials while your video waits to render."
                    project.media_progress = MediaProgress(
                        stage="waiting", step_count=len(plan.steps[:max_media_steps]),
                        ready_count=0, elapsed_seconds=0,
                    )
                    await emit("status", {"status": project.status.value, "message": project.status_message,
                                          "media_progress": project.media_progress.model_dump()})
                async with media_slot or nullcontext():
                    await generate_media_for_plan(
                        project=project,
                        gemini=gemini,
                        publish_event=publish_event,
                        max_steps=max_media_steps,
                        before_completion=commerce,
                    )
        if not generate_media:
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
