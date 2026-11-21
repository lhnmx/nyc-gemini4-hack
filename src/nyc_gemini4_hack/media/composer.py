from __future__ import annotations

import asyncio
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg


def _wav_duration_seconds(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate) if rate else 6.0


async def compose_step_pov_video(
    frames: list[Path],
    voice_wav: Path,
    out_mp4: Path,
    min_duration: float = 5.5,
) -> Path:
    """
    Compose 3 continuity-chained POV frames + voiceover WAV into a smooth 1280x720
    H.264/AAC loopable POV step MP4 with gentle Ken Burns motion and crossfades.
    """
    if len(frames) < 1:
        raise ValueError("At least 1 POV frame is required to compose a video.")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    audio_dur = _wav_duration_seconds(voice_wav)
    total_dur = max(min_duration, audio_dur + 0.8)
    n = len(frames)
    fade_dur = 0.45 if n > 1 else 0.0
    # Each clip duration so total after (n-1) crossfades equals total_dur
    clip_dur = (total_dur + fade_dur * (n - 1)) / n
    fps = 24
    clip_frames = max(12, int(round(clip_dur * fps)))

    def _run() -> None:
        cmd: list[str] = [ffmpeg, "-y"]
        for f in frames:
            cmd.extend(["-loop", "1", "-t", f"{clip_dur:.2f}", "-i", str(f)])
        cmd.extend(["-i", str(voice_wav)])

        filter_parts: list[str] = []
        for idx in range(n):
            # Subtle POV breathing zoom (1.0 -> 1.05) centered, scaled to 1280x720
            filter_parts.append(
                f"[{idx}:v]scale=1600:900:force_original_aspect_ratio=increase,"
                f"crop=1600:900,"
                f"zoompan=z='min(zoom+0.0005,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={clip_frames}:s=1280x720:fps={fps},format=yuv420p,setsar=1[v{idx}]"
            )

        if n == 1:
            video_out = "[v0]"
        else:
            prev_label = "v0"
            for idx in range(1, n):
                offset = max(0.1, idx * (clip_dur - fade_dur))
                out_label = f"xf{idx}"
                filter_parts.append(
                    f"[{prev_label}][v{idx}]xfade=transition=fade:duration={fade_dur:.2f}:offset={offset:.2f}[{out_label}]"
                )
                prev_label = out_label
            video_out = f"[{prev_label}]"

        filter_complex = ";".join(filter_parts)
        audio_idx = n

        cmd.extend(
            [
                "-filter_complex",
                filter_complex,
                "-map",
                video_out,
                "-map",
                f"{audio_idx}:a",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                "-t",
                f"{total_dur:.2f}",
                str(out_mp4),
            ]
        )

        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    await asyncio.to_thread(_run)
    return out_mp4


async def stitch_full_tutorial(step_mp4s: list[Path], out_mp4: Path) -> Path:
    """Concatenate all individual POV step MP4s into one continuous tutorial video."""
    if not step_mp4s:
        raise ValueError("No step MP4s provided to stitch.")

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    concat_file = out_mp4.parent / "concat_list.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in step_mp4s),
        encoding="utf-8",
    )

    def _run() -> None:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    await asyncio.to_thread(_run)
    return out_mp4
