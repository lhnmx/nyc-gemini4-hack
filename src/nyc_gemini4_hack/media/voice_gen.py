from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg

from nyc_gemini4_hack.gemini_client import Gemini4Client

logger = logging.getLogger(__name__)


def _pcm_to_wav(pcm_bytes: bytes, out_path: Path, sample_rate: int = 24000) -> Path:
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return out_path


async def _generate_gemini_audio(
    script: str,
    out_wav: Path,
    gemini: Gemini4Client,
    timeout_s: float = 15.0,
) -> Path | None:
    """Try native Gemini Interactions API audio generation (gemini-omni-flash-preview)."""
    model = gemini.settings.audio_model
    if not hasattr(gemini.client, "interactions"):
        return None

    def _call() -> object:
        return gemini.client.interactions.create(
            model=model,
            api_version="v1beta",
            input=(
                "Speak ONLY the following craft instruction clearly, warmly, and naturally "
                "at a calm instructional pace (do not add any extra words):\n"
                f"{script}"
            ),
            response_modalities=["audio"],
        )

    try:
        resp = await asyncio.wait_for(asyncio.to_thread(_call), timeout=timeout_s)
    except Exception as exc:
        logger.warning("Gemini native audio (%s) unavailable/timed out (%s); using TTS fallback.", model, exc)
        return None

    outputs = getattr(resp, "outputs", None) or []
    for item in outputs:
        data = getattr(item, "data", None)
        mime = (getattr(item, "mime_type", None) or "").lower()
        if not data:
            continue
        raw = base64.b64decode(data) if isinstance(data, str) else bytes(data)
        if "pcm" in mime or "l16" in mime:
            return _pcm_to_wav(raw, out_wav, sample_rate=24000)
        if "wav" in mime:
            out_wav.write_bytes(raw)
            return out_wav
        # Convert mp3/ogg/etc. to WAV via bundled ffmpeg
        tmp_path = out_wav.with_suffix(".raw_audio")
        tmp_path.write_bytes(raw)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run(
            [ffmpeg, "-y", "-i", str(tmp_path), "-ar", "44100", "-ac", "1", str(out_wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        tmp_path.unlink(missing_ok=True)
        return out_wav

    return None


async def _generate_macos_tts(script: str, out_wav: Path) -> Path:
    """Fast, zero-latency local macOS neural voice fallback converted to WAV via ffmpeg."""
    say_bin = shutil.which("say")
    if not say_bin:
        raise RuntimeError("No TTS backend available (Gemini audio failed and macOS 'say' not found).")

    aiff_path = out_wav.with_suffix(".aiff")
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def _run() -> None:
        # Try a pleasant voice; fall back to default system voice if not installed
        res = subprocess.run(
            [say_bin, "-v", "Samantha", "-r", "172", "-o", str(aiff_path), script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if res.returncode != 0:
            subprocess.run(
                [say_bin, "-r", "172", "-o", str(aiff_path), script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            [ffmpeg, "-y", "-i", str(aiff_path), "-ar", "44100", "-ac", "1", str(out_wav)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        aiff_path.unlink(missing_ok=True)

    await asyncio.to_thread(_run)
    return out_wav


async def generate_step_voiceover(
    script: str,
    out_wav: Path,
    gemini: Gemini4Client,
    prefer_gemini_audio: bool = True,
) -> Path:
    """
    Generate spoken voiceover for a build step.
    Attempts native Gemini Interactions audio first, falling back seamlessly to
    local TTS so the hackathon demo never blocks or loses voice narration.
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    if prefer_gemini_audio:
        native = await _generate_gemini_audio(script, out_wav, gemini)
        if native is not None and native.exists() and native.stat().st_size > 1000:
            return native
    return await _generate_macos_tts(script, out_wav)
