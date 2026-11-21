import base64
import json
import subprocess
import tempfile
import unittest
from array import array
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import imageio_ffmpeg
from google import genai

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.motion_gen import compose_motion_with_voice, generate_motion_shot, generate_step_motion
from nyc_gemini4_hack.models import BuildStep, PhysicalStateTransition, VisualContinuityBible
from tests.test_video import decode, write_tone


def moving_shot(path: Path) -> Path:
    # A white bar travels from left to right each frame, over a constant provider soundtrack.
    scene = "color=c=black:s=160x90:r=24:d=1,geq=lum='if(between(X,6*N,6*N+14),220,20)':cb=128:cr=128"
    subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i", scene,
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=1", "-c:v", "libx264", "-c:a", "aac", str(path),
    ], check=True)
    return path


def bar_locations(path: Path) -> list[float]:
    pixels = decode(path, ["-an", "-vf", "scale=80:1", "-pix_fmt", "gray",
                           "-fps_mode", "passthrough", "-f", "rawvideo"])
    centers = []
    for offset in range(0, len(pixels), 80):
        white = [x for x, value in enumerate(pixels[offset:offset + 80]) if value > 130]
        centers.append(sum(white) / len(white))
    return centers


class MotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.bible = VisualContinuityBible(workspace_surface="maple bench", lighting="left window",
                                           camera_style="fixed overhead", subject_palette="navy cloth",
                                           hands_description="short nails and brown skin")
        self.step = BuildStep(
            step_number=1, title="Turn the hem", instruction_text="Fold the hem under.",
            narration_script="Turn the raw edge under.", pov_video_prompt="The thumb lifts the raw edge.",
            physical_state=PhysicalStateTransition(start_state="cloth with exposed edge",
                hand_kinematics="Pin the cloth with the left hand and turn the edge with the right thumb.",
                end_state="a doubled hem", verification_cues=["parallel edges"]), duration_seconds=2,
        )

    async def client_with(self, handler):
        settings = Settings(_env_file=None, gemini_api_key="dummy", pov_video_model="gemini-omni-1.2-flash")
        client = Gemini4Client(settings)
        transport = httpx.MockTransport(handler)
        client._client = genai.Client(api_key="dummy", http_options={
            "httpx_client": self.enterContext(httpx.Client(transport=transport)),
            "httpx_async_client": await self.enterAsyncContext(httpx.AsyncClient(transport=transport)),
        })
        self.addAsyncCleanup(client.aclose)
        return client

    async def test_real_sdk_downloads_film_and_sends_previous_result_as_opening_image(self):
        video = moving_shot(self.folder / "source.mp4").read_bytes()
        requests = []
        file_states = iter(["PROCESSING", "ACTIVE"])
        uri = "https://generativelanguage.googleapis.com/v1beta/files/shot123:download?alt=media"

        def transport(request):
            path = request.url.path
            if path.endswith("/interactions"):
                requests.append(json.loads(request.content))
                payload = ({"uri": uri} if len(requests) == 1
                           else {"data": base64.b64encode(video).decode("ascii")})
                return httpx.Response(200, json={"id": "clip", "status": "completed", "steps": [{
                    "type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4", **payload}],
                }]})
            if path.endswith("/files/shot123"):
                return httpx.Response(200, json={"name": "files/shot123", "state": next(file_states)})
            self.assertTrue(path.endswith("/files/shot123:download"), path)
            return httpx.Response(200, content=video)

        client = await self.client_with(transport)
        with patch("nyc_gemini4_hack.media.motion_gen.asyncio.sleep", new_callable=AsyncMock):
            motion = await generate_motion_shot(self.step, self.bible, self.folder, client)
        self.assertEqual(motion.video.read_bytes(), video)
        self.assertEqual(motion.frames, 24)
        self.assertAlmostEqual(bar_locations(motion.end_frame)[0], bar_locations(motion.video)[-1], delta=1)
        voice = self.folder / "voice.wav"
        write_tone(voice, 0.2, 24000)
        film, poster, end = await compose_motion_with_voice(motion, voice, self.folder / "chapter.mp4")
        self.assertEqual(end, motion.end_frame)

        # A later wait can start from the actual result of the previous physical manipulation.
        wait = self.step.model_copy(update={"step_number": 2, "duration_seconds": 45, "title": "Let the dye dry"})
        second = await generate_step_motion(wait, self.bible, self.folder, client, end)
        self.assertEqual(second.read_bytes(), video)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["model"], "gemini-omni-1.2-flash")
        self.assertIs(requests[0]["store"], True)
        self.assertEqual(requests[0]["response_format"], {
            "type": "video", "aspect_ratio": "16:9", "duration": "3s", "resolution": "720p", "delivery": "uri",
        })
        self.assertEqual(requests[1]["response_format"]["duration"], "15s")
        self.assertEqual([block["type"] for block in requests[0]["input"][0]["content"]], ["text"])
        first, text = requests[1]["input"][0]["content"]
        self.assertEqual(base64.b64decode(first["data"]), end.read_bytes())
        self.assertEqual(first["mime_type"], "image/jpeg")
        self.assertIn("preceding action", text["text"])
        self.assertIn(self.step.physical_state.start_state, text["text"])
        self.assertIn(self.step.physical_state.end_state, text["text"])
        self.assertIn("preserve the visible setup", text["text"])
        self.assertIn("one continuous manipulation", requests[0]["input"][0]["content"][0]["text"])
        self.assertNotIn("keep hands out of frame", requests[0]["input"][0]["content"][0]["text"])
        self.assertIn(self.bible.hands_description, requests[0]["input"][0]["content"][0]["text"])
        self.assertNotIn(self.bible.hands_description, text["text"])
        for detail in [self.step.physical_state.hand_kinematics,
                       self.step.physical_state.verification_cues[0], "keep hands out of frame"]:
            self.assertIn(detail, text["text"])

        centers = bar_locations(film)
        self.assertEqual(len(centers), 24)
        self.assertEqual(len(set(centers)), 24)
        self.assertAlmostEqual(bar_locations(poster)[0], centers[12], delta=1)
        self.assertAlmostEqual(bar_locations(end)[0], centers[-1], delta=1)
        samples = array("h")
        samples.frombytes(decode(film, ["-vn", "-ar", "8000", "-f", "s16le"]))
        self.assertGreater(max(samples[100:1200]), 1000)
        self.assertLess(max(abs(s) for s in samples[4500:6800]), 20)

    async def test_holds_result_until_long_narration_finishes_then_preserves_previous_render_on_failure(self):
        motion = moving_shot(self.folder / "valid.mp4")
        voice = self.folder / "long.wav"
        write_tone(voice, 1.8, 24000)
        film, poster, end = await compose_motion_with_voice(motion, voice, self.folder / "chapter.mp4")
        centers = bar_locations(film)
        self.assertEqual(len(centers), 53)
        self.assertEqual(len(set(centers[:24])), 24)
        self.assertTrue(all(abs(x - centers[23]) < 1 for x in centers[24:]))
        audio = array("h")
        audio.frombytes(decode(film, ["-vn", "-ar", "8000", "-f", "s16le"]))
        self.assertGreater(max(audio[13400:14000]), 1000)
        self.assertLess(max(abs(x) for x in audio[15400:16400]), 20)
        originals = {path: path.read_bytes() for path in (film, poster, end)}
        motion.write_bytes(b"incomplete file")
        with self.assertRaisesRegex(RuntimeError, "no usable motion"):
            await compose_motion_with_voice(motion, voice, film)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    async def test_bad_provider_response_cannot_replace_previous_motion(self):
        existing = self.folder / "step_01_motion.mp4"
        existing.write_bytes(b"previous shot")
        def transport(request):
            return httpx.Response(200, json={"id": "clip", "status": "completed", "steps": [{
                "type": "model_output", "content": [{"type": "video", "mime_type": "video/mp4", "data": "YWJj"}],
            }]})
        client = await self.client_with(transport)
        with self.assertRaisesRegex(RuntimeError, "no usable motion"):
            await generate_step_motion(self.step, self.bible, self.folder, client)
        self.assertEqual(existing.read_bytes(), b"previous shot")
        self.assertEqual(list(self.folder.iterdir()), [existing])
