from __future__ import annotations

import asyncio
import base64
import math
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg
from google.genai import types

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import _render, _wav_duration_seconds
from nyc_gemini4_hack.media.video_gen import _image_format
from nyc_gemini4_hack.models import BuildStep, VisualContinuityBible

FPS = 24
MOTION_TIMEOUT = 300.0


def _shot_prompt(step: BuildStep, bible: VisualContinuityBible) -> str:
    state = step.physical_state
    return (
        "Film one continuous, photorealistic first-person craft demonstration. Lock the camera so "
        "the learner can follow the contact point and the whole workpiece throughout the movement. "
        "For manual work, show the same adult maker's human hands enter, grip, perform the full "
        "technique at a readable pace, and release. Show how the work is supported and how "
        "the fingers or tool transmit pressure to the material. Keep wrists connected, fingers "
        "anatomically consistent, and contact visible. Materials bend, turn or join only as the "
        "maker moves them. During passive drying, cooling or waiting, keep hands out of frame and "
        "the work undisturbed. End with a brief clear view of the result. Keep the shot free "
        "of graphics, cuts, dialogue, music and on-screen text.\n"
        f"View: {bible.camera_style}. Surface: {bible.workspace_surface}. Light: {bible.lighting}. "
        f"Palette: {bible.subject_palette}. Maker's hands: {bible.hands_description}.\n"
        f"Action: {step.title}. {step.instruction_text}\n"
        f"Begin with {state.start_state}\n"
        f"Demonstrate {state.hand_kinematics}\n"
        f"Visible motion: {step.pov_video_prompt}\n"
        f"Finish with {state.end_state}\n"
        f"Visible checks: {', '.join(state.verification_cues)}"
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
    prompt = _shot_prompt(step, bible)
    contents = []
    if prev_step_end_frame is not None:
        data = prev_step_end_frame.read_bytes()
        mime_type, _ = _image_format(data)
        contents.append({"type": "image", "mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")})
        prompt = ("Begin this shot with the attached final frame of the preceding action. Match the "
                  "maker, tools, viewpoint and placement of completed work.\n" + prompt)
    contents.append({"type": "text", "text": prompt})

    # Concurrent speech generation means exact audio length is known at composition time.
    # Estimate a relaxed teaching cadence now so most narration plays over moving footage.
    speech_seconds = math.ceil(len(step.narration_script.split()) / 2.3 + 0.4)
    duration = max(3, min(15, max(step.duration_seconds, speech_seconds)))
    model = gemini.settings.pov_video_model
    out_dir.mkdir(parents=True, exist_ok=True)
    output = out_dir / f"step_{step.step_number:02d}_motion.mp4"
    with TemporaryDirectory(dir=out_dir) as staging:
        video = Path(staging) / "motion.mp4"
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
        await asyncio.to_thread(_motion_frames, video)
        video.replace(output)
    return output


def _motion_frames(video: Path) -> int:
    """Count decodable frames on the same 24 fps timeline used to encode the chapter."""
    result = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
        "-i", str(video), "-map", "0:v:0", "-vf", f"setpts=PTS-STARTPTS,fps={FPS}",
        "-progress", "pipe:1", "-nostats", "-f", "null", "-",
    ], capture_output=True, text=True, errors="replace")
    frames = [int(line.removeprefix("frame=")) for line in result.stdout.splitlines() if line.startswith("frame=")]
    if result.returncode or not frames or frames[-1] < 2:
        raise RuntimeError(f"The generated demonstration has no usable motion footage. {result.stderr[-400:].strip()}")
    return frames[-1]


async def compose_motion_with_voice(
    video: Path,
    voice_wav: Path,
    out_mp4: Path,
    min_duration: float = 0.0,
) -> tuple[Path, Path, Path]:
    """Return the narrated chapter, the action poster and the frame to carry into the next shot."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    poster = out_mp4.with_name(f"{out_mp4.stem}_poster.jpg")
    end_frame = out_mp4.with_name(f"{out_mp4.stem}_end.jpg")

    def render() -> None:
        source_frames = _motion_frames(video)
        minimum = max(min_duration, _wav_duration_seconds(voice_wav) + 0.4)
        frames = max(source_frames, math.ceil(minimum * FPS))
        with TemporaryDirectory(dir=out_mp4.parent) as staging:
            directory = Path(staging)
            shot = directory / out_mp4.name
            action = directory / poster.name
            end = directory / end_frame.name
            # The two JPEGs come from the real motion before any padding. Hold only the final
            # frame when narration needs more time. Discard the provider's own soundtrack.
            filters = (
                f"[0:v:0]setpts=PTS-STARTPTS,fps={FPS},scale=1280:720:force_original_aspect_ratio=decrease,"
                "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,split=3[shot][act][end];"
                f"[act]select=eq(n\\,{source_frames // 2})[poster];"
                f"[end]select=eq(n\\,{source_frames - 1})[last];"
                f"[shot]tpad=stop_mode=clone:stop={frames - source_frames}[chapter]"
            )
            _render([
                "-i", str(video), "-i", str(voice_wav), "-filter_complex", filters,
                "-map", "[poster]", "-frames:v", "1", "-q:v", "2", "-update", "1", str(action),
                "-map", "[last]", "-frames:v", "1", "-q:v", "2", "-update", "1", str(end),
                "-map", "[chapter]", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "22", "-pix_fmt", "yuv420p", "-af", "apad", "-ar", "48000", "-ac", "1",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{frames / FPS:.8f}",
            ], shot)
            if not all(path.is_file() and path.stat().st_size for path in (shot, action, end)):
                raise RuntimeError("The narrated demonstration is missing a rendered frame.")
            action.replace(poster)
            end.replace(end_frame)
            shot.replace(out_mp4)

    await asyncio.to_thread(render)
    return out_mp4, poster, end_frame
