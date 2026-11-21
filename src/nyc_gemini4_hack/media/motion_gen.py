from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import imageio_ffmpeg
from google.genai import types

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import CHAPTER_FPS, _render, _wav_duration_seconds, run_media_work
from nyc_gemini4_hack.media.shot_review import corrective_target, discrepancies, inspect_shot
from nyc_gemini4_hack.media.video_gen import _image_format, source_observations
from nyc_gemini4_hack.models import BuildStep, ShotReview, VisualContinuityBible

FPS = CHAPTER_FPS
MOTION_TIMEOUT = 300.0
TIMELINE = f"setpts=PTS-STARTPTS,fps={FPS}"
CHAPTER_FORMAT = ("scale=1280:720:force_original_aspect_ratio=decrease,"
                  "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MotionShot:
    video: Path
    frames: int
    end_frame: Path
    reviews: tuple[ShotReview, ...]
    selected_attempt: int | None = None

    @property
    def selected_review(self) -> ShotReview | None:
        if self.selected_attempt is not None:
            return next((review for review in self.reviews if review.attempt == self.selected_attempt), None)
        return self.reviews[-1] if self.reviews else None

    @property
    def continuity_frame(self) -> Path | None:
        review = self.selected_review
        return self.end_frame if review and review.status == "passed" else None


class MotionOutputError(RuntimeError):
    """A description of a missing or unusable video that is safe to display in a guide."""


def _shot_prompt(step: BuildStep, bible: VisualContinuityBible, has_reference: bool) -> str:
    state = step.physical_state
    setup = (
        "Use the attached opening reference as the exact first frame. Continue from "
        "the positions of the actual hands and workpiece in that image, including all completed work. "
        "If the written starting description differs, preserve the visible setup for frame one. "
        "The material must then change to the specified finishing geometry in full view."
        if has_reference else
        f"View: {bible.camera_style}. Surface: {bible.workspace_surface}. Light: {bible.lighting}. "
        f"Palette: {bible.subject_palette}. Maker's hands: {bible.hands_description}."
    )
    passive = re.match(r"^(?:wait\b|let\b.*\b(?:dry|cool|cure|set|rest|harden)\b|allow\b.*\bto (?:dry|cool|cure|set|rest|harden)\b)",
                       step.title, re.IGNORECASE)
    performance = (
        "During the wait, keep hands out of frame and the work undisturbed."
        if passive else
        "Show one continuous manipulation using the demonstrated grip or tool. Keep the workpiece and "
        "the point of contact visible. Show any needed support while the active fingers or tool move the "
        "material. Every intermediate position follows from that contact. Show each specified repetition in order."
    )
    source = source_observations(step)
    source_detail = f"\nObserved source technique:\n{source}" if source else ""
    cues = ", ".join(state.verification_cues)
    return (
        "One photorealistic first-person instructional shot, locked camera, uninterrupted real-time motion. "
        "Keep the subject in focus in the same framing. No cuts, shape jumps, added parts, captions or music.\n"
        f"{setup}\n"
        f"Starting geometry: {state.start_state}\n"
        f"Action — {step.title}: {state.hand_kinematics}\n"
        f"{performance} Visual direction: {step.pov_video_prompt}.{source_detail}\n"
        f"Hold the visible result still for the final beat: {state.end_state}. Visible checks: {cues}"
    )


async def _download_video(gemini: Gemini4Client, uri: str, output: Path) -> None:
    files = gemini.client.aio.files
    while True:
        info = await files.get(name=uri)
        if info.state == types.FileState.ACTIVE:
            await files.download(file=uri, destination=output)
            return
        if info.state != types.FileState.PROCESSING:
            raise MotionOutputError(f"The video provider could not prepare the file ({info.state}).")
        await asyncio.sleep(3)


async def generate_step_motion(
    step: BuildStep,
    bible: VisualContinuityBible,
    out_dir: Path,
    gemini: Gemini4Client,
    prev_step_end_frame: Path | None = None,
) -> Path:
    """Generate one Omni motion shot using the preceding shot's final frame when available."""
    return (await generate_motion_shot(step, bible, out_dir, gemini, prev_step_end_frame)).video


async def generate_motion_shot(
    step: BuildStep,
    bible: VisualContinuityBible,
    out_dir: Path,
    gemini: Gemini4Client,
    prev_step_end_frame: Path | None = None,
    speech_seconds: float | None = None,
) -> MotionShot:
    """Prefer imagery that passes review and retain the first decodable take as a fallback."""
    prompt = _shot_prompt(step, bible, prev_step_end_frame is not None)
    if speech_seconds is None:
        speech_seconds = len(step.narration_script.split()) / 2.3
    duration = max(3, min(15, max(step.duration_seconds, math.ceil(speech_seconds + 0.4))))
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"step_{step.step_number:02d}_motion.mp4"
    end_frame = out_dir / f"step_{step.step_number:02d}_pov_end.jpg"
    reviews: list[ShotReview] = []
    opening = prev_step_end_frame
    target = target_origin = None
    target_preparation_seconds = None
    with TemporaryDirectory(dir=out_dir) as staging:
        preferred = None
        try:
            async with asyncio.timeout(MOTION_TIMEOUT):
                for attempt in (1, 2):
                    directory = Path(staging) / str(attempt)
                    directory.mkdir()
                    started = monotonic()
                    video = directory / "motion.mp4"
                    await _render_candidate(gemini, prompt, duration, opening, target, video)
                    count, samples = await run_media_work(_sample_motion, video, directory)
                    candidate = (video, count, samples[-1][1], attempt)
                    if preferred is None:
                        preferred = candidate
                    review = await inspect_shot(
                        gemini, step, samples, attempt=attempt, render_seconds=monotonic() - started,
                        reference_mode="first_last_frames" if target else "first_frame" if opening else "text",
                        target_origin=target_origin,
                    )
                    if target_preparation_seconds is not None:
                        review.target_image_model = gemini.settings.pov_image_model
                        review.target_preparation_seconds = target_preparation_seconds
                    reviews.append(review)
                    logger.info("Step %s attempt %s: render %.2fs, visual review %.2fs (%s)",
                                step.step_number, attempt, review.render_seconds, review.review_seconds, review.status)
                    if review.status == "passed" or (attempt == 1 and review.status != "rejected"):
                        preferred = candidate
                        break
                    if attempt == 2:
                        break
                    # A rejected shot stays in staging. Reuse only a boundary individually confirmed by the reviewer.
                    problem = discrepancies(review)
                    opening = samples[0][1] if review.opening.verdict == "matches" else None
                    if opening and review.result.verdict == "matches":
                        target, target_origin = samples[-1][1], "reviewed_render"
                    elif opening:
                        preparing = monotonic()
                        target = await corrective_target(gemini, step, opening, directory, problem)
                        target_preparation_seconds = round(monotonic() - preparing, 3)
                        target_origin = "reviewed_illustration" if target else None
                    prompt = (_shot_prompt(step, bible, opening is not None)
                              + "\nCorrect the following observed discrepancy in a fresh take:\n" + problem)
        except Exception as exc:
            if preferred is None:
                raise
            logger.info("Step %s using its decodable first take after %s", step.step_number, type(exc).__name__)

        video, count, frame, attempt = preferred
        frame.replace(end_frame)
        video.replace(output)
        return MotionShot(output, count, end_frame, tuple(reviews), selected_attempt=attempt)


async def _render_candidate(gemini: Gemini4Client, prompt: str, duration: int,
                            opening: Path | None, target: Path | None, video: Path) -> None:
    contents = []
    for path in (opening, target):
        if path is not None:
            data = path.read_bytes()
            mime_type, _ = _image_format(data)
            contents.append({"type": "image", "mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")})
    if target is not None:
        # Omni 1.2 takes two ordered images; its two-frame interpolation mode supports 3–10s.
        duration = min(duration, 10)
        prompt += "\nUse the first image as the first frame and the second as the exact last frame."
    contents.append({"type": "text", "text": prompt})
    model = gemini.settings.pov_video_model
    result = await gemini.client.aio.interactions.create(
        model=model, api_version="v1beta", input=contents,
        response_format={"type": "video", "duration": f"{duration}s", "aspect_ratio": "16:9",
                         "resolution": "720p", "delivery": "uri"},
        store=True, timeout=MOTION_TIMEOUT,
    )
    media = result.output_video
    if result.status != "completed" or media is None:
        raise MotionOutputError(f"The video provider returned no completed shot ({result.status}).")
    if media.uri:
        await _download_video(gemini, media.uri, video)
    elif media.data:
        video.write_bytes(base64.b64decode(media.data, validate=True))
    else:
        raise MotionOutputError("The video provider returned an empty motion shot.")


def _sample_motion(video: Path, directory: Path) -> tuple[int, list[tuple[float, Path]]]:
    frames = _motion_frames(video)
    indices = sorted({round(i * (frames - 1) / 6) for i in range(7)})
    selection = "+".join(f"eq(n\\,{i})" for i in indices)
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
        "-i", str(video), "-map", "0:v:0", "-vf", f"{TIMELINE},select={selection},{CHAPTER_FORMAT}",
        "-fps_mode", "passthrough", "-q:v", "2", "-frames:v", str(len(indices)), str(directory / "sample_%02d.jpg"),
    ], capture_output=True, check=True)
    samples = [(round(n / FPS, 4), directory / f"sample_{i:02d}.jpg") for i, n in enumerate(indices, 1)]
    if not all(path.is_file() and path.stat().st_size for _, path in samples):
        raise ValueError("Could not decode the requested demonstration frames.")
    return frames, samples


def _motion_frames(video: Path) -> int:
    """Count decodable frames on the same 24 fps timeline used to encode the chapter."""
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
        "-i", str(video), "-map", "0:v:0", "-vf", TIMELINE,
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ], capture_output=True, text=True, errors="replace")
    frames = [int(line.removeprefix("frame=")) for line in result.stdout.splitlines() if line.startswith("frame=")]
    if result.returncode or not frames or frames[-1] < 2:
        raise MotionOutputError("The generated video had no usable motion frames.")
    return frames[-1]


def _prepare_reference(video: Path, end_frame: Path) -> int:
    frames = _motion_frames(video)
    # Count on the chapter timeline so variable frame rates, decoder reordering and
    # a longer source soundtrack cannot shift the continuity reference to an earlier frame.
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
        "-i", str(video), "-map", "0:v:0", "-vf", f"{TIMELINE},select=eq(n\\,{frames - 1}),{CHAPTER_FORMAT}",
        "-frames:v", "1", "-q:v", "2", "-update", "1", str(end_frame),
    ], capture_output=True, check=True)
    if not end_frame.is_file() or not end_frame.stat().st_size:
        raise ValueError("The demonstration is missing its final frame.")
    return frames


async def compose_motion_with_voice(
    video: Path | MotionShot,
    voice_wav: Path,
    out_mp4: Path,
    min_duration: float = 0.0,
) -> tuple[Path, Path, Path]:
    """Return the narrated chapter, the action poster and the frame to carry into the next shot."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    poster = out_mp4.with_name(f"{out_mp4.stem}_poster.jpg")
    end_frame = video.end_frame if isinstance(video, MotionShot) else out_mp4.with_name(f"{out_mp4.stem}_end.jpg")

    def render() -> None:
        with TemporaryDirectory(dir=out_mp4.parent) as staging:
            directory = Path(staging)
            shot = directory / out_mp4.name
            action = directory / poster.name
            end = directory / end_frame.name
            if isinstance(video, MotionShot):
                source, source_frames = video.video, video.frames
            else:
                source, source_frames = video, _prepare_reference(video, end)
            minimum = max(min_duration, _wav_duration_seconds(voice_wav) + 0.4)
            frames = max(source_frames, math.ceil(minimum * FPS))
            # The action poster is from the real motion, ahead of any narration padding.
            filters = (
                f"[0:v:0]{TIMELINE},{CHAPTER_FORMAT},split=2[shot][act];"
                f"[act]select=eq(n\\,{source_frames // 2})[poster];"
                f"[shot]tpad=stop_mode=clone:stop={frames - source_frames}[chapter]"
            )
            _render([
                "-i", str(source), "-i", str(voice_wav), "-filter_complex", filters,
                "-map", "[poster]", "-frames:v", "1", "-q:v", "2", "-update", "1", str(action),
                "-map", "[chapter]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "22", "-pix_fmt", "yuv420p", "-af", "apad", "-ar", "48000", "-ac", "1",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{frames / FPS:.8f}",
            ], shot)
            if not all(path.is_file() and path.stat().st_size for path in (shot, action)):
                raise RuntimeError("The narrated demonstration is missing a rendered frame.")
            action.replace(poster)
            if not isinstance(video, MotionShot):
                end.replace(end_frame)
            shot.replace(out_mp4)

    await run_media_work(render)
    return out_mp4, poster, end_frame
