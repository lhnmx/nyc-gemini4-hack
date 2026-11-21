from __future__ import annotations

import asyncio
import math
import subprocess
import wave
from dataclasses import dataclass
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg

CHAPTER_FPS = 24


async def run_media_work(function, /, *args):
    """Join local file/FFmpeg work before cancellation can release its workspace."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


def _wav_duration_seconds(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        return wf.getnframes() / wf.getframerate()


@dataclass(frozen=True)
class NarratedChapter:
    video: Path
    frames: int
    speech_seconds: float
    transcript: str

    @property
    def duration_seconds(self) -> float:
        return self.frames / CHAPTER_FPS


def _vtt_timestamp(milliseconds: int) -> str:
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


def write_tutorial_captions(chapters: list[NarratedChapter], output: Path) -> list[float]:
    """Place each spoken instruction on the same frame timeline as the film."""
    cues = ["WEBVTT\n\n"]
    starts = []
    elapsed_frames = 0
    for chapter in chapters:
        start = elapsed_frames / CHAPTER_FPS
        starts.append(start)
        start_ms = round(start * 1000)
        end_ms = max(start_ms + 1, round((start + chapter.speech_seconds) * 1000))
        text = escape(" ".join(chapter.transcript.split()), quote=False)
        cues.append(f"{_vtt_timestamp(start_ms)} --> {_vtt_timestamp(end_ms)}\n{text}\n\n")
        elapsed_frames += chapter.frames
    with TemporaryDirectory(dir=output.parent) as staging:
        candidate = Path(staging) / output.name
        candidate.write_text("".join(cues), encoding="utf-8")
        candidate.replace(output)
    return starts


def caption_chapter(video: Path, voice_wav: Path | None, transcript: str) -> NarratedChapter:
    """Measure the encoded chapter and time its caption to the recorded narration."""
    frames, _ = imageio_ffmpeg.count_frames_and_secs(video)
    if frames < 1:
        raise ValueError(f"No video frames in {video.name}.")
    duration = frames / CHAPTER_FPS
    # Older saved renders can contain just the narrated MP4.
    speech = min(duration, _wav_duration_seconds(voice_wav)) if voice_wav else duration
    chapter = NarratedChapter(video, frames, speech, transcript)
    write_tutorial_captions([chapter], video.with_suffix(".vtt"))
    return chapter


def _render(cmd: list[str], out_mp4: Path) -> None:
    # Keep the previous playable file intact if an encode fails partway through.
    with TemporaryDirectory(dir=out_mp4.parent) as staging:
        output = Path(staging) / "render.mp4"
        result = subprocess.run(
            [imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror",
             *cmd, str(output)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        if result.returncode:
            raise RuntimeError(f"FFmpeg could not produce {out_mp4.name}: {result.stderr.strip()[-1200:]}")
        output.replace(out_mp4)


async def compose_step_pov_video(
    frames: list[Path],
    voice_wav: Path,
    out_mp4: Path,
    min_duration: float = 5.5,
) -> Path:
    """Compose a 720p POV clip with crossfades and enough time to hear every word."""
    if not frames:
        raise ValueError("At least 1 POV frame is required to compose a video.")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    fps = CHAPTER_FPS
    count = len(frames)
    duration = max(min_duration, _wav_duration_seconds(voice_wav) + 0.8)
    total_frames = max(count + 1, math.ceil(duration * fps))
    fade_frames = max(1, min(round(0.45 * fps), total_frames // (2 * count)))
    # Count overlapping frames twice when allocating time to each still.
    base, extra = divmod(total_frames + fade_frames * (count - 1), count)
    clip_lengths = [base + (idx < extra) for idx in range(count)]

    cmd = []
    filters = []
    for idx, frame in enumerate(frames):
        cmd.extend(["-i", str(frame)])
        # Each input is one still. zoompan emits exactly this many output frames.
        filters.append(
            f"[{idx}:v]scale=1600:900:force_original_aspect_ratio=decrease,"
            "pad=1600:900:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            "zoompan=z='min(zoom+0.0005,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={clip_lengths[idx]}:s=1280x720:fps={fps},format=yuv420p[v{idx}]"
        )
    cmd.extend(["-i", str(voice_wav)])

    output = "v0"
    elapsed_frames = clip_lengths[0]
    for idx in range(1, count):
        offset = (elapsed_frames - fade_frames) / fps
        filters.append(
            f"[{output}][v{idx}]xfade=transition=fade:duration={fade_frames / fps:.8f}"
            f":offset={offset:.8f}[xf{idx}]"
        )
        elapsed_frames += clip_lengths[idx] - fade_frames
        output = f"xf{idx}"

    # Every step has the same audio format and a full-length audio stream so a
    # stitched tutorial preserves the pause and narration onset at each boundary.
    cmd.extend([
        "-filter_complex", ";".join(filters), "-map", f"[{output}]", "-map", f"{count}:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-af", "apad", "-ar", "48000", "-ac", "1", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", "-t", f"{total_frames / fps:.8f}",
    ])
    await run_media_work(_render, cmd, out_mp4)
    return out_mp4


async def stitch_full_tutorial(
    step_mp4s: list[Path], out_mp4: Path, *, durations: list[float] | None = None,
) -> Path:
    """Concatenate the individual POV clips without re-encoding their video."""
    if not step_mp4s:
        raise ValueError("No step MP4s provided to stitch.")
    if durations is not None and len(durations) != len(step_mp4s):
        raise ValueError("Each clip needs its measured duration.")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)

    def _run() -> None:
        with TemporaryDirectory(dir=out_mp4.parent) as staging:
            concat_file = Path(staging) / "clips.ffconcat"
            lines = []
            for i, path in enumerate(step_mp4s):
                escaped = path.resolve().as_posix().replace("'", "'\\''")
                lines.append(f"file '{escaped}'\n")
                if durations is not None:
                    lines.append(f"duration {durations[i]:.9f}\n")
            concat_file.write_text("".join(lines), encoding="utf-8")
            # AAC priming packets start before zero. Preserve the video's frame-zero
            # timestamp and rebuild the audio clock at joins so chapter seeks stay exact.
            _render([
                "-copyts", "-f", "concat", "-safe", "0", "-i", str(concat_file),
                "-c:v", "copy", "-af", "aresample=async=1:first_pts=0",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            ], out_mp4)

    await run_media_work(_run)
    return out_mp4
