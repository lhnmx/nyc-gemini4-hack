import base64
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import imageio_ffmpeg
from google import genai

from nyc_gemini4_hack import pipeline
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import ProjectState, ProjectStatus
from tests.test_motion import bar_locations, moving_shot, sample_findings
from tests.test_pipeline import paper_plan
from tests.test_video import PNG_FRAMES, write_tone
from tests.test_voice import wav_bytes


class ProviderFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_serializes_real_sdk_requests_and_renders_playable_project(self):
        await self.render_project()

    async def test_explicit_illustration_mode_serializes_real_image_requests(self):
        await self.render_project(illustrated=True)

    async def test_failed_video_recovers_with_reviewed_images_in_a_playable_mixed_film(self):
        await self.render_project(recover=True)

    async def render_project(self, illustrated=False, recover=False):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = Settings(_env_file=None, media_output_dir=folder, gemini_api_key="offline",
                            gemini_model_carbon="research", gemini_model_argon="plan",
                            gemini_model_hydrogen="ground", pov_image_model="illustrate")
        if illustrated:
            settings.pov_media_mode = "illustrated_frames"
        url = "https://www.youtube.com/watch?v=abcdefghijk"
        plan = paper_plan().model_dump()
        plan["steps"] = plan["steps"][:2]
        if recover:
            plan["steps"][1]["duration_seconds"] = 2
            plan["steps"][1]["source_segments"] = ["S1.1"]
        if illustrated:
            plan["steps"] = plan["steps"][:1]
            plan["steps"][0]["duration_seconds"] = 1
        extract = {
            "video_id": "abcdefghijk", "url": url, "title": "First fold",
            "segments": [{"timestamp_start": "00:00", "timestamp_end": "00:05",
                          "technique": "Folding", "hand_action": "Crease the paper"}],
        }
        ground = {"continuity_bible": plan["continuity_bible"], "steps": [
            {key: step[key] for key in ("step_number", "pov_video_prompt", "physical_state")}
            for step in plan["steps"]
        ]}
        images = iter(PNG_FRAMES)
        moving = None if illustrated else base64.b64encode(moving_shot(folder / "moving.mp4").read_bytes()).decode()
        voice = folder / "voice.wav"
        write_tone(voice, .65, 24000)
        audio = iter([voice.read_bytes(), wav_bytes()])
        seen = []

        def provider(request):
            body = json.loads(request.content)
            path = request.url.path
            if path.endswith("/interactions"):
                kind = body["response_format"]["type"]
                seen.append(kind)
                if kind == "video":
                    self.assertEqual(body["model"], "gemini-omni-1.2-flash")
                    if recover and seen.count("video") == 2:
                        return httpx.Response(200, json={"id": "empty", "status": "completed", "steps": []})
                    part = {"type": "video", "mime_type": "video/mp4", "data": moving}
                else:
                    part = {"type": "audio", "mime_type": "audio/wav", "data": base64.b64encode(next(audio)).decode()}
                return httpx.Response(200, json={
                    "id": kind, "status": "completed", "steps": [{"type": "model_output", "content": [part]}],
                })
            model = path.rsplit("/", 1)[1].split(":")[0]
            if body.get("generationConfig", {}).get("responseSchema", {}).get("title") == "_Observations":
                self.assertEqual(model, "research")
                seen.append("review")
                findings = sample_findings()
                if recover and seen.count("review") == 2:
                    parts = body["contents"][0]["parts"]
                    received = [base64.urlsafe_b64decode(x["inlineData"]["data"]) for x in parts if "inlineData" in x]
                    self.assertEqual(received, list(PNG_FRAMES))
                    self.assertIn("Crease the paper", parts[0]["text"])
                    for i, field in enumerate(("opening", "contact", "result"), 1):
                        findings[field]["sample"] = i
                part = {"text": json.dumps(findings)}
                return httpx.Response(200, json={"candidates": [
                    {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}
                ]})
            seen.append(model)
            if model == "illustrate":
                if recover:
                    prompt = body["contents"][0]["parts"][-1]["text"]
                    self.assertIn("Crease the paper", prompt)
                part = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(next(images)).decode()}}
            else:
                self.assertIn("responseSchema", body["generationConfig"])
                part = {"text": json.dumps({"research": extract, "plan": plan, "ground": ground}[model])}
            return httpx.Response(200, json={"candidates": [
                {"content": {"role": "model", "parts": [part]}, "finishReason": "STOP"}
            ]})

        # Only the transport is replaced. The SDK still builds the actual requests and
        # validates provider responses. Motion, audio and FFmpeg run normally.
        transport = httpx.MockTransport(provider)
        sync_http = self.enterContext(httpx.Client(transport=transport))
        async_http = await self.enterAsyncContext(httpx.AsyncClient(transport=transport))
        client = Gemini4Client(settings)
        client._client = genai.Client(api_key="offline", http_options={
            "httpx_client": sync_http, "httpx_async_client": async_http,
        })
        self.enterContext(patch.object(pipeline, "Gemini4Client", return_value=client))
        self.enterContext(patch.object(pipeline, "get_settings", return_value=settings))
        state = ProjectState(project_id="test-build", goal="Fold a glider")
        await pipeline.run_project_pipeline(state, direct_urls=[url])
        self.assertEqual(state.status, ProjectStatus.COMPLETED, state.status_message)
        self.assertEqual(seen[:3], ["research", "plan", "ground"])
        self.assertEqual(state.plan.source_youtube_urls, [url])
        for media_url in [state.media[1].keyframe_url, state.media[1].audio_url,
                          state.media[1].video_url, state.media[1].captions_url,
                          state.full_video_url, state.full_captions_url]:
            self.assertGreater((folder / media_url.removeprefix("/media/")).stat().st_size, 0)
        if illustrated:
            self.assertCountEqual(seen[3:], ["illustrate"] * 3 + ["audio"])
            return
        if recover:
            self.assertCountEqual(seen[3:], ["video", "audio", "review"] * 2 + ["illustrate"] * 3)
            first, replacement = state.media[1], state.media[2]
            self.assertEqual((first.media_mode, replacement.media_mode), ("motion", "illustrated_frames"))
            self.assertEqual(replacement.video_model, settings.pov_image_model)
            self.assertIn("no completed shot", replacement.motion_failure)
            self.assertFalse(replacement.shot_reviews)
            self.assertEqual(replacement.illustration_review.status, "passed")
            self.assertEqual(replacement.illustration_review.source_segment_ids, ["S1.1"])
            self.assertEqual(replacement.chapter_start_seconds, first.duration_seconds)
            film = folder / state.full_video_url.removeprefix("/media/")
            self.assertEqual(imageio_ffmpeg.count_frames_and_secs(film)[0],
                             round((first.duration_seconds + replacement.duration_seconds) * 24))
            captions = (folder / state.full_captions_url.removeprefix("/media/")).read_text()
            self.assertLess(captions.index(plan["steps"][0]["narration_script"]),
                            captions.index(plan["steps"][1]["narration_script"]))
            for url in (replacement.video_url, replacement.audio_url, replacement.captions_url, replacement.keyframe_url):
                self.assertGreater((folder / url.removeprefix("/media/")).stat().st_size, 0)
            return
        self.assertCountEqual(seen[3:], ["video", "audio", "review"] * 2)
        self.assertEqual([a.duration_seconds for a in state.media.values()], [26/24, 1])
        self.assertEqual([a.chapter_start_seconds for a in state.media.values()], [0, 26/24])
        film = folder / state.full_video_url.removeprefix("/media/")
        centers = bar_locations(film)
        self.assertEqual(len(centers), 50)
        self.assertGreater(abs(centers[26] - centers[25]), 50)
        self.assertAlmostEqual(centers[26], centers[0], delta=1)
        # Inspect the timestamp on chapter two's first moving frame inside the finished MP4.
        probe = subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-i", str(film), "-an", "-vf", r"select=eq(n\,26),showinfo",
            "-f", "null", "-",
        ], capture_output=True, text=True, check=True)
        self.assertAlmostEqual(float(re.search(r"pts_time:([\d.]+)", probe.stderr)[1]),
                               state.media[2].chapter_start_seconds, places=5)
        captions = (folder / state.full_captions_url.removeprefix("/media/")).read_text()
        self.assertIn("00:00:00.000 --> 00:00:00.650\nFold the sheet", captions)
        self.assertIn("00:00:01.083 --> 00:00:01.087\nFold the corners inward", captions)
