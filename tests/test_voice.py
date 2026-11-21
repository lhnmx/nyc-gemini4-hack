import asyncio
import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from google import genai

from nyc_gemini4_hack.media import voice_gen


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\x01\x00" * 96)
    return output.getvalue()


class VoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.wav = Path(self.temp.name) / "narration.wav"
        self.line = "Fold the flap until its edge meets the crease."
        self.settings = SimpleNamespace(audio_model="gemini-3.8-flash-tts", tts_voice="Kore")

    async def test_sdk_sends_only_transcript_as_speech_and_decodes_response(self):
        requests = []
        recording = wav_bytes()

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={
                "id": "example", "status": "completed",
                "steps": [{"type": "model_output", "content": [{
                    "type": "audio", "mime_type": "audio/wav",
                    "data": base64.b64encode(recording).decode(),
                }]}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with genai.Client(api_key="offline-test", http_options={"httpx_async_client": http}) as sdk:
                gemini = SimpleNamespace(client=sdk, settings=self.settings)
                await voice_gen.generate_step_voiceover(self.line, self.wav, gemini, duration_seconds=8)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/v1beta/interactions")
        body = json.loads(requests[0].content)
        self.assertEqual(body["model"], self.settings.audio_model)
        self.assertEqual(body["generation_config"]["speech_config"], [{"voice": "Kore"}])
        spoken = body["input"][0]["content"][0]
        self.assertEqual(spoken["text"], self.line)
        self.assertEqual(spoken["annotations"][0]["type"], "speech_metadata")
        self.assertIn("8 seconds", spoken["annotations"][0]["style"])
        self.assertEqual(self.wav.read_bytes(), recording)

    async def test_pcm_metadata_controls_real_playback_duration(self):
        samples = b"\x01\x00\x02\x00" * 8000
        for extra, mime in [
            ({"sample_rate": 16000, "channels": 2}, "audio/l16"),
            ({}, "audio/pcm;rate=16000;channels=2"),
        ]:
            with self.subTest(mime=mime):
                voice_gen._write_audio(SimpleNamespace(data=samples, mime_type=mime, **extra), self.wav)
                with wave.open(str(self.wav)) as recording:
                    self.assertEqual(recording.getframerate(), 16000)
                    self.assertEqual(recording.getnchannels(), 2)
                    self.assertEqual(recording.getnframes(), 8000)
                    self.assertEqual(recording.readframes(8000), samples)

    async def test_invalid_service_recording_falls_back_without_overwriting_prior_audio(self):
        prior = wav_bytes()
        self.wav.write_bytes(prior)
        for bad, mime in [
            (b"not a WAV", "audio/wav"), (prior[:-8], "audio/wav"),
            ("%%%", "audio/wav"), (b"x", "audio/l16"), (b"", "audio/wav"),
        ]:
            with self.subTest(mime=mime, size=len(bad)):
                service = AsyncMock(return_value=SimpleNamespace(output_audio=SimpleNamespace(data=bad, mime_type=mime)))
                gemini = SimpleNamespace(settings=self.settings, client=SimpleNamespace(
                    aio=SimpleNamespace(interactions=SimpleNamespace(create=service))))
                with patch.object(voice_gen, "_generate_local_tts", side_effect=RuntimeError("unavailable")) as fallback:
                    with self.assertRaisesRegex(RuntimeError, "unavailable"):
                        await voice_gen.generate_step_voiceover(self.line, self.wav, gemini)
                fallback.assert_called_once()
                self.assertEqual(self.wav.read_bytes(), prior)
                self.assertEqual(list(self.wav.parent.iterdir()), [self.wav])

    async def test_timeout_cancels_outstanding_request(self):
        cancelled = asyncio.Event()

        async def never(**kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        gemini = SimpleNamespace(settings=self.settings, client=SimpleNamespace(
            aio=SimpleNamespace(interactions=SimpleNamespace(create=never))))
        with self.assertRaises(TimeoutError):
            await voice_gen._generate_gemini_audio(self.line, self.wav, gemini, 6, timeout_s=0.01)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.wav.exists())

    async def test_local_voice_accepts_a_script_as_text_and_rejects_empty_output(self):
        recording = wav_bytes()
        received = []

        def speak(cmd, *, input, **kwargs):
            received.append((cmd, input))
            Path(cmd[cmd.index("-w") + 1]).write_bytes(recording)

        with patch.object(voice_gen.shutil, "which", side_effect=lambda name: "/bin/espeak" if name == "espeak" else None):
            with patch.object(voice_gen.subprocess, "run", side_effect=speak):
                await voice_gen.generate_step_voiceover(self.line, self.wav, None, prefer_gemini_audio=False)
            self.assertEqual(received[0][1].strip(), self.line)
            self.assertNotIn(self.line, received[0][0])
            recording = recording[:44]
            with patch.object(voice_gen.subprocess, "run", side_effect=speak):
                with self.assertRaisesRegex(ValueError, "empty or incomplete"):
                    await voice_gen.generate_step_voiceover(self.line, self.wav, None, prefer_gemini_audio=False)
            self.assertEqual(self.wav.read_bytes(), wav_bytes())
