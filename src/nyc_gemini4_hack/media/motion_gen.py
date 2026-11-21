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
from nyc_gemini4_hack.media.video_gen import _image_format
from nyc_gemini4_hack.models import BuildStep, VisualContinuityBible

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


def _shot_prompt(step: BuildStep, bible: VisualContinuityBible, has_reference: bool) -> str:
    state = step.physical_state
    setup = (
        "Use the attached final frame of the preceding action as the exact first frame. Continue from "
        "the positions of the actual hands and workpiece in that image, including all completed work. "
        "If the written description differs, preserve the visible setup."
        if has_reference else
        f"View: {bible.camera_style}. Surface: {bible.workspace_surface}. Light: {bible.lighting}. "
        f"Palette: {bible.subject_palette}. Maker's hands: {bible.hands_description}."
    )
    passive = re.match(r"^(?:wait\b|let\b.*\b(?:dry|cool|cure|set|rest|harden)\b|allow\b.*\bto (?:dry|cool|cure|set|rest|harden)\b)",
                       step.title, re.IGNORECASE)
    performance = (
        "During the wait, keep hands out of frame and the work undisturbed."
        if passive else
        "Show one continuous manipulation by the same pair of human hands. Keep the entire workpiece and "
        "the point of contact visible. The supporting hand steadies the work while the active fingers or tool "
        "physically move it. Every intermediate position follows from that contact. Keep the wrists attached "
        "and the finger count constant. Show each specified repetition in order."
    )
    paper = (" For paper, preserve its outline and every existing crease. Only the lifted layer pivots "
             "around the new fold; a fingertip presses the crease once the edges align."
             if re.search(r"\b(?:paper|origami|cardstock)\b", f"{bible.subject_palette} {state.start_state}", re.I)
             and re.search(r"\b(?:fold|folding|crease)\b", f"{step.title} {state.hand_kinematics}", re.I) else "")
    cue = f" Visible check: {state.verification_cues[0]}" if state.verification_cues else ""
    return (
        "One photorealistic first-person instructional shot, locked camera, uninterrupted real-time motion. "
        "Keep the subject in focus in the same framing. No cuts, shape jumps, added parts, captions or music.\n"
        f"{setup}\n"
        f"Starting geometry: {state.start_state}\n"
        f"Action — {step.title}: {state.hand_kinematics}\n"
        f"{performance}{paper}\n"
        f"Hold the visible result still for the final beat: {state.end_state}.{cue}"
    )


async def _download_video(gemini: Gemini4Client, uri: str, output: Path) -> None:
    files = gemini.client.aio.files
    while True:
        info = await files.get(name=uri)
        if info.state == types.FileState.ACTIVE:
            await files.download(file=uri, destination=output)
            return
        if info.state != types.FileState.PROCESSING:
            raise RuntimeError(f"The video provider could not prepare the file ({info.state}).")
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
    """Release a verified shot and its real final frame before chapter encoding starts."""
    prompt = _shot_prompt(step, bible, prev_step_end_frame is not None)
    contents = []
    if prev_step_end_frame is not None:
        data = prev_step_end_frame.read_bytes()
        mime_type, _ = _image_format(data)
        contents.append({"type": "image", "mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")})
    contents.append({"type": "text", "text": prompt})

    # A prefetched recording can give a better duration without delaying the provider request.
    if speech_seconds is None:
        speech_seconds = len(step.narration_script.split()) / 2.3
    duration = max(3, min(15, max(step.duration_seconds, math.ceil(speech_seconds + 0.4))))
    model = gemini.settings.pov_video_model
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"step_{step.step_number:02d}_motion.mp4"
    end_frame = out_dir / f"step_{step.step_number:02d}_pov_end.jpg"
    with TemporaryDirectory(dir=out_dir) as staging:
        video = Path(staging) / "motion.mp4"
        started = monotonic()
        async with asyncio.timeout(MOTION_TIMEOUT):
            result = await gemini.client.aio.interactions.create(
                model=model,
                api_version="v1beta",
                input=contents,
                response_format={"type": "video", "duration": f"{duration}s", "aspect_ratio": "16:9",
                                 "resolution": "720p", "delivery": "uri"},
                store=True,  # URI delivery requires a stored interaction.
                timeout=MOTION_TIMEOUT,
            )
            generated = monotonic()
            media = result.output_video
            if result.status != "completed" or media is None:
                raise RuntimeError(f"Video model {model} returned no completed motion shot ({result.status}).")
            if media.uri:
                await _download_video(gemini, media.uri, video)
            elif media.data:
                video.write_bytes(base64.b64decode(media.data, validate=True))
            else:
                raise RuntimeError(f"Video model {model} returned an empty motion shot.")
        # Check container and decode before publishing a replacement, including on a retry.
        downloaded = monotonic()
        end = Path(staging) / "end.jpg"
        frames = await run_media_work(_prepare_reference, video, end)
        end.replace(end_frame)
        video.replace(output)
        logger.info("Step %s motion (%ss requested): generation %.2fs, retrieval %.2fs, final frame %.2fs",
                    step.step_number, duration, generated - started, downloaded - generated, monotonic() - downloaded)
    return MotionShot(output, frames, end_frame)


def _motion_frames(video: Path) -> int:
    """Count decodable frames on the same 24 fps timeline used to encode the chapter."""
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
        "-i", str(video), "-map", "0:v:0", "-vf", TIMELINE,
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ], capture_output=True, text=True, errors="replace")
    frames = [int(line.removeprefix("frame=")) for line in result.stdout.splitlines() if line.startswith("frame=")]
    if result.returncode or not frames or frames[-1] < 2:
        raise RuntimeError(f"The generated demonstration has no usable motion footage. {result.stderr[-400:].strip()}")
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
