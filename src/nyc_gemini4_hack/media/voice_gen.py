from __future__ import annotations

import asyncio
import base64
import logging
import shutil
import subprocess
import wave
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg

from nyc_gemini4_hack.gemini_client import Gemini4Client

logger = logging.getLogger(__name__)


def _check_wav(path: Path) -> None:
    with wave.open(str(path), "rb") as recording:
        frames = recording.getnframes()
        frame_size = recording.getnchannels() * recording.getsampwidth()
        if not frames or recording.getframerate() <= 0:
            raise ValueError("The narration recording is empty or incomplete.")
        if len(recording.readframes(frames)) != frames * frame_size:
            raise ValueError("The narration recording is empty or incomplete.")


def _write_audio(audio, out_wav: Path) -> None:
    if not audio or not audio.data:
        raise ValueError("The speech model returned no audio.")
    raw = base64.b64decode(audio.data, validate=True) if isinstance(audio.data, str) else bytes(audio.data)
    content_type = Message()
    content_type["Content-Type"] = audio.mime_type or "audio/wav"
    mime = content_type.get_content_type()
    if mime in {"audio/wav", "audio/wave", "audio/x-wav"}:
        out_wav.write_bytes(raw)
    elif mime in {"audio/pcm", "audio/l16"}:
        # Gemini returns signed little-endian samples, including for its audio/l16 format.
        rate = getattr(audio, "sample_rate", None)
        channels = getattr(audio, "channels", None)
        if rate is None:
            rate = int(content_type.get_param("rate", 24000))
        if channels is None:
            channels = int(content_type.get_param("channels", 1))
        if rate <= 0 or channels <= 0 or len(raw) % (2 * channels):
            raise ValueError("Invalid PCM metadata or incomplete audio frame.")
        with wave.open(str(out_wav), "wb") as recording:
            recording.setparams((channels, 2, rate, 0, "NONE", "not compressed"))
            recording.writeframes(raw)
    elif mime in {"audio/mpeg", "audio/mp3", "audio/ogg", "audio/ogg_opus"}:
        encoding = "ogg" if mime.startswith("audio/ogg") else "mp3"
        subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", encoding,
            "-i", "pipe:0", "-ar", "24000", "-ac", "1", str(out_wav),
        ], input=raw, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=True)
    else:
        raise ValueError(f"Unsupported speech audio format: {mime}")
    _check_wav(out_wav)


async def _generate_gemini_audio(
    script: str,
    out_wav: Path,
    gemini: Gemini4Client,
    duration_seconds: int,
    timeout_s: float = 30.0,
) -> None:
    style = (
        "Speak like a patient craft teacher beside one learner. Start immediately and stress the action verb. "
        f"Aim for a natural reading in about {duration_seconds} seconds. Give measurements time to land."
    )
    async with asyncio.timeout(timeout_s):
        response = await gemini.client.aio.interactions.create(
            model=gemini.settings.audio_model,
            api_version="v1beta",
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text", "text": script,
                    "annotations": [{"type": "speech_metadata", "style": style}],
                }],
            }],
            response_format={"type": "audio", "mime_type": "audio/wav"},
            generation_config={"speech_config": [{"voice": gemini.settings.tts_voice}]},
            timeout=timeout_s,
        )
    await asyncio.to_thread(_write_audio, response.output_audio, out_wav)


def _generate_local_tts(script: str, out_wav: Path) -> None:
    if say := shutil.which("say"):
        cmd = [
            say, "-r", "150", "--file-format=WAVE", "--data-format=LEI16@24000",
            "-o", str(out_wav),
        ]
    elif espeak := shutil.which("espeak-ng") or shutil.which("espeak"):
        cmd = [espeak, "-s", "150", "-w", str(out_wav), "--stdin"]
    else:
        raise RuntimeError("Local narration requires macOS say or eSpeak.")
    # stdin keeps the transcript separate from voice options.
    subprocess.run(cmd, input=script + "\n", text=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, check=True, timeout=30)
    _check_wav(out_wav)


async def generate_step_voiceover(
    script: str,
    out_wav: Path,
    gemini: Gemini4Client,
    prefer_gemini_audio: bool = True,
    duration_seconds: int = 6,
) -> Path:
    """Speak the script verbatim. Publish a WAV only after its audio is usable."""
    script = script.strip()
    if not script or duration_seconds < 1:
        raise ValueError("Narration needs a nonempty script and a positive duration.")
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out_wav.parent) as staging:
        candidate = Path(staging) / "voice.wav"
        if prefer_gemini_audio:
            try:
                await _generate_gemini_audio(script, candidate, gemini, duration_seconds)
            except Exception as exc:
                logger.warning("Gemini narration unavailable (%s); trying local speech.", type(exc).__name__)
                candidate.unlink(missing_ok=True)
            else:
                candidate.replace(out_wav)
                return out_wav
        await asyncio.to_thread(_generate_local_tts, script, candidate)
        candidate.replace(out_wav)
    return out_wav
