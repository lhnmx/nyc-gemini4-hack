import asyncio
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
from nyc_gemini4_hack.media.motion_gen import (
    ShotRejected, _shot_prompt, compose_motion_with_voice, generate_motion_shot, generate_step_motion,
)
from nyc_gemini4_hack.models import BuildStep, PhysicalStateTransition, StepSourceSegment, VisualContinuityBible
from tests.test_video import PNG_FRAMES, decode, write_tone


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


def sample_findings():
    # Canned audit for transport/assembly fixtures. Pixel-dependent verdicts have separate coverage below.
    return {"opening": {"verdict": "matches", "sample": 1, "observation": "Marker at left stop."},
            "contact": {"verdict": "matches", "sample": 4, "observation": "Marker crosses the center."},
            "result": {"verdict": "matches", "sample": 7, "observation": "Marker at right stop."}}


def structured_response(payload):
    return httpx.Response(200, json={"candidates": [
        {"content": {"role": "model", "parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}
    ]})


def motion_response(video):
    return httpx.Response(200, json={"id": "clip", "status": "completed", "steps": [{
        "type": "model_output", "content": [{
            "type": "video", "mime_type": "video/mp4", "data": base64.b64encode(video).decode("ascii"),
        }],
    }]})


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

    def test_motion_prompt_uses_the_steps_own_source_contact_and_result(self):
        reference = StepSourceSegment(segment_id="S1.4", source_url="https://youtu.be/01234567890",
                                      source_title="Hem setup", start_seconds=14, end_seconds=21,
                                      technique="Turn a narrow hem", hand_action="Thumb rolls the raw edge behind.",
                                      notes="The doubled band hides the raw threads.")
        self.step.source_segments = [reference]
        prompt = _shot_prompt(self.step, self.bible, True)
        for detail in ("for frame one", reference.hand_action, reference.notes,
                       self.step.physical_state.end_state, self.step.pov_video_prompt):
            self.assertIn(detail, prompt)

    async def test_real_sdk_downloads_film_and_sends_previous_result_as_opening_image(self):
        video = moving_shot(self.folder / "source.mp4").read_bytes()
        requests = []
        file_states = iter(["PROCESSING", "ACTIVE"])
        uri = "https://generativelanguage.googleapis.com/v1beta/files/shot123:download?alt=media"

        def transport(request):
            path = request.url.path
            if path.endswith(":generateContent"):
                body = json.loads(request.content)
                parts = body["contents"][0]["parts"]
                self.assertEqual(sum("inlineData" in part for part in parts), 7)
                return structured_response(sample_findings())
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
        self.assertIn("opening reference", text["text"])
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

    async def test_review_reads_rendered_pixels_and_retries_wrong_endpoint_with_a_checked_target(self):
        # Same start and composition: one take remains at the left stop; the other travels right.
        moving = moving_shot(self.folder / "moving.mp4")
        stationary = self.folder / "stationary.mp4"
        subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(moving), "-an",
            "-vf", r"select=eq(n\,0),loop=loop=23:size=1:start=0", "-frames:v", "24", str(stationary),
        ], check=True)
        endpoint = self.folder / "target.jpg"
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-i", str(moving),
                        "-vf", r"select=eq(n\,23)", "-frames:v", "1", "-update", "1", str(endpoint)], check=True)
        # This setting exercises the 10s two-image limit, even for a requested 15s chapter.
        self.step.duration_seconds = 15
        self.step.title = "Observe the slider travel to the right stop"
        self.step.pov_video_prompt = "Follow the bright marker from left to right."
        self.step.physical_state = PhysicalStateTransition(
            start_state="Bright marker at the left edge of its black track.",
            hand_kinematics="Let the mechanism carry the marker across the track in full view.",
            end_state="Bright marker at the right end of the track.",
            verification_cues=["Its right edge lies in the final fifth of the track."],
        )
        self.step.source_segments = [StepSourceSegment(segment_id="S1.1", source_url="https://youtu.be/01234567890",
            source_title="Track", start_seconds=4, end_seconds=8, technique="Inspect travel",
            hand_action="Marker travels to the right limit.", notes="The left end is empty.")]
        takes, observations, targets = [], [], []

        def position(encoded):
            check_frame = self.folder / "observed.jpg"
            check_frame.write_bytes(base64.urlsafe_b64decode(encoded))
            return bar_locations(check_frame)[0]

        def transport(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/interactions"):
                takes.append(body)
                return motion_response((stationary if len(takes) == 1 else moving).read_bytes())
            parts = body["contents"][0]["parts"]
            payloads = [p["inlineData"]["data"] for p in parts if "inlineData" in p]
            if "responseSchema" not in body["generationConfig"]:
                self.assertLess(position(payloads[0]), 12)
                self.assertIn("left stop", parts[-1]["text"])
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
                    "mimeType": "image/jpeg", "data": base64.b64encode(endpoint.read_bytes()).decode(),
                }}]}}]})
            if len(payloads) == 2:
                targets.append([position(encoded) for encoded in payloads])
                matches = targets[-1][0] < 12 and targets[-1][1] > 64
                return structured_response({"verdict": "matches" if matches else "contradicts",
                                            "observation": "Marker at right limit of the same track."})
            self.assertIn("The left end is empty.", parts[0]["text"])
            xs = [position(encoded) for encoded in payloads]
            observations.append(xs)
            findings = sample_findings()
            if xs[-1] < 64:
                findings["result"].update(verdict="contradicts", observation="Marker still at the left stop.")
            return structured_response(findings)

        client = await self.client_with(transport)
        motion = await generate_motion_shot(self.step, self.bible, self.folder, client)
        self.assertEqual([r.status for r in motion.reviews], ["rejected", "passed"])
        self.assertEqual([r.source_segment_ids for r in motion.reviews], [["S1.1"], ["S1.1"]])
        self.assertEqual([r.attempt for r in motion.reviews], [1, 2])
        self.assertEqual(motion.reviews[1].target_origin, "reviewed_illustration")
        self.assertEqual(motion.reviews[1].sampled_seconds, [0, 0.1667, 0.3333, 0.5, 0.625, 0.7917, 0.9583])
        self.assertEqual(len(takes), 2)
        self.assertEqual(len(targets), 1)
        self.assertEqual(takes[0]["response_format"]["duration"], "15s")
        self.assertEqual(takes[1]["response_format"]["duration"], "10s")
        first, last, prompt = takes[1]["input"][0]["content"]
        self.assertLess(position(first["data"]), 12)
        self.assertGreater(position(last["data"]), 64)
        self.assertIn("Marker still at the left stop.", prompt["text"])
        self.assertLess(max(observations[0]), 12)
        self.assertGreater(observations[1][-1], 64)
        self.assertEqual(motion.video.read_bytes(), moving.read_bytes())
        self.assertEqual(motion.continuity_frame, motion.end_frame)

    async def test_unclear_or_unavailable_review_does_not_trigger_a_retry_or_chain_a_frame(self):
        video = moving_shot(self.folder / "moving.mp4").read_bytes()
        for status in ("uncertain", "unavailable", "invalid_evidence"):
            renders = []
            def transport(request):
                if request.url.path.endswith("/interactions"):
                    renders.append(1)
                    return motion_response(video)
                if status == "unavailable":
                    return httpx.Response(403, json={"error": {"code": 403, "message": "disabled"}})
                findings = sample_findings()
                findings["contact"].update(verdict="unclear", observation="The contact point is covered.")
                if status == "invalid_evidence":
                    findings["result"].update(verdict="contradicts", sample=42)
                return structured_response(findings)
            with self.subTest(status=status):
                motion = await generate_motion_shot(self.step, self.bible, self.folder, await self.client_with(transport))
                expected = "unavailable" if status == "invalid_evidence" else status
                self.assertEqual([r.status for r in motion.reviews], [expected])
                self.assertIsNone(motion.continuity_frame)
                self.assertEqual(len(renders), 1)
                self.assertEqual(motion.video.read_bytes(), video)

    async def test_unresolved_contact_fault_spends_one_retry_and_never_replaces_existing_motion(self):
        video = moving_shot(self.folder / "moving.mp4").read_bytes()
        existing = self.folder / "step_01_motion.mp4"
        existing.write_bytes(b"prior usable shot")
        previous_end = self.folder / "step_01_pov_end.jpg"
        previous_end.write_bytes(b"prior usable reference")
        for second_status in ("rejected", "uncertain", "unavailable"):
            renders = []
            def transport(request):
                if request.url.path.endswith("/interactions"):
                    renders.append(json.loads(request.content))
                    return motion_response(video)
                findings = sample_findings()
                if len(renders) == 2 and second_status == "unavailable":
                    return httpx.Response(403, json={"error": {"code": 403, "message": "disabled"}})
                if len(renders) == 2 and second_status == "uncertain":
                    findings["contact"].update(verdict="unclear", observation="Thumb hidden by cloth.")
                else:
                    findings["contact"].update(verdict="contradicts", observation="Presser foot is visibly lifted.")
                return structured_response(findings)
            with self.subTest(second_status=second_status):
                client = await self.client_with(transport)
                with self.assertRaises(ShotRejected) as failure:
                    await generate_motion_shot(self.step, self.bible, self.folder, client)
                self.assertEqual([r.status for r in failure.exception.reviews], ["rejected", second_status])
                self.assertEqual(failure.exception.reviews[-1].target_origin, "reviewed_render")
                self.assertIn("Presser foot", renders[1]["input"][0]["content"][-1]["text"])
                self.assertEqual(len(renders), 2)
                self.assertEqual(existing.read_bytes(), b"prior usable shot")
                self.assertEqual(previous_end.read_bytes(), b"prior usable reference")

    async def test_rejected_target_is_excluded_from_the_correction_request(self):
        video = moving_shot(self.folder / "moving.mp4").read_bytes()
        renders = []
        target_verdicts = []
        def transport(request):
            body = json.loads(request.content)
            if request.url.path.endswith("/interactions"):
                renders.append(body)
                return motion_response(video)
            if "responseSchema" not in body["generationConfig"]:
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"inlineData": {
                    "mimeType": "image/png", "data": base64.b64encode(PNG_FRAMES[0]).decode(),
                }}]}}]})
            images = [p for p in body["contents"][0]["parts"] if "inlineData" in p]
            if len(images) == 2:
                target_verdicts.append("contradicts")
                return structured_response({"verdict": "contradicts", "observation": "Solid red image."})
            findings = sample_findings()
            if len(renders) == 1:
                findings["result"].update(verdict="contradicts", observation="The fold remains open.")
            return structured_response(findings)

        client = await self.client_with(transport)
        motion = await generate_motion_shot(self.step, self.bible, self.folder, client)
        self.assertEqual(target_verdicts, ["contradicts"])
        self.assertEqual([p["type"] for p in renders[1]["input"][0]["content"]], ["image", "text"])
        self.assertEqual(motion.reviews[-1].reference_mode, "first_frame")
        self.assertIsNone(motion.reviews[-1].target_origin)
        self.assertIsNotNone(motion.reviews[-1].target_preparation_seconds)

    async def test_cancel_during_visual_review_joins_request_and_discards_staged_files(self):
        video = moving_shot(self.folder / "moving.mp4").read_bytes()
        started, finished = asyncio.Event(), asyncio.Event()
        async def transport(request):
            if request.url.path.endswith("/interactions"):
                return motion_response(video)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        client = await self.client_with(transport)
        render = asyncio.create_task(generate_motion_shot(self.step, self.bible, self.folder, client))
        await asyncio.wait_for(started.wait(), 3)
        render.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await render
        self.assertTrue(finished.is_set())
        self.assertEqual([p.name for p in self.folder.iterdir()], ["moving.mp4"])

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
