import asyncio
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

from nyc_gemini4_hack import cloud_worker, pipeline
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.media import composer
from nyc_gemini4_hack.models import (
    BuildPlan, FrameFinding, MaterialItem, ProjectState, ProjectStatus, ShoppingCartProposal, ShotReview,
)
from tests.test_video import PNG_FRAMES


def paper_plan():
    steps = [
        {
            "step_number": number, "title": title, "instruction_text": title,
            "narration_script": title, "pov_video_prompt": title, "duration_seconds": 7,
            "physical_state": {"start_state": before, "hand_kinematics": title, "end_state": after},
        }
        for number, (title, before, after) in enumerate([
            ("Fold the sheet", "One flat sheet", "Creased sheet"),
            ("Fold the corners inward", "Creased sheet", "Pointed nose"),
            ("Open the wings", "Pointed nose", "Spread wings"),
        ], start=1)
    ]
    return BuildPlan(
        project_title="Paper glider", steps=steps,
        continuity_bible={"workspace_surface": "oak", "lighting": "daylight",
                          "camera_style": "fixed POV", "subject_palette": "white paper"},
    )


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(_env_file=None, media_output_dir=self.folder, max_parallel_shots=2)
        self.enterContext(patch.object(pipeline, "get_settings", return_value=self.settings))
        self.project = ProjectState(project_id="craft-1", goal="Fold a paper glider")
        self.plan = paper_plan()
        self.client = self.enterContext(patch.object(pipeline, "Gemini4Client"))
        self.client.return_value.aclose = AsyncMock()
        self.client.return_value.settings = self.settings
        self.client.return_value.model_id.return_value = "visual-reviewer"
        self.research = self.enterContext(patch.object(
            pipeline, "research_goal_on_youtube", new=AsyncMock(return_value=([], [])),
        ))
        self.planner = self.enterContext(patch.object(
            pipeline, "synthesize_build_plan", new=AsyncMock(return_value=self.plan),
        ))
        self.events = []
        self.references = []

        async def motion(*, step, bible, out_dir, gemini, prev_step_end_frame, speech_seconds):
            self.references.append(prev_step_end_frame)
            video = out_dir / f"{step.step_number}-motion.mp4"
            review = ShotReview(attempt=1, status="passed", model="fixture", reference_mode="text",
                                render_seconds=1, review_seconds=0.1)
            return pipeline.MotionShot(video, 24, out_dir / f"{step.step_number}-motion-end.jpg", (review,))

        async def voice(*, script, out_wav, gemini, prefer_gemini_audio, duration_seconds, on_voice_model):
            self.assertTrue(prefer_gemini_audio)
            self.assertEqual(duration_seconds, 7)
            with wave.open(str(out_wav), "wb") as audio:
                audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                audio.writeframes(bytes(8000))  # Half a second of recorded narration
            on_voice_model(self.settings.audio_model)
            return out_wav

        async def compose(*, video, voice_wav, out_mp4):
            out_mp4.touch()
            return out_mp4, video.video.with_suffix(".jpg"), video.end_frame

        async def stitch(clips, output, *, durations):
            self.assertEqual([clip.name for clip in clips], [f"step_{i:02d}_pov.mp4" for i in (1, 2, 3)])
            self.assertEqual(durations, [25/24, 26/24, 27/24])
            self.assertTrue(all(clip.is_file() for clip in clips))
            output.touch()
            return output

        self.motion = self.enterContext(patch.object(pipeline, "generate_motion_shot", side_effect=motion))
        self.frames = self.enterContext(patch.object(pipeline, "generate_step_pov_frames",
            side_effect=RuntimeError("Image provider unavailable")))
        self.voice = self.enterContext(patch.object(pipeline, "generate_step_voiceover", side_effect=voice))
        self.compose = self.enterContext(patch.object(pipeline, "compose_motion_with_voice", side_effect=compose))
        self.enterContext(patch.object(composer.imageio_ffmpeg, "count_frames_and_secs", side_effect=lambda path: (
            24 + int(path.name.split("_")[1]), 0,
        )))
        self.stitch = self.enterContext(patch.object(pipeline, "stitch_full_tutorial", side_effect=stitch))

    async def capture(self, project_id, event, data):
        self.assertEqual(project_id, self.project.project_id)
        self.events.append((event, data))

    async def run_project(self, **options):
        return await pipeline.run_project_pipeline(self.project, publish_event=self.capture, **options)

    async def recovery_frames(self, *, out_dir, step, **kwargs):
        frames = []
        for i, image in enumerate(PNG_FRAMES, 1):
            path = out_dir / f"step_{step.step_number:02d}_frame_{i}.png"
            path.write_bytes(image)
            frames.append(path)
        return frames

    async def test_slow_cart_is_checkpointed_after_film_and_before_worker_release(self):
        self.plan.materials = [MaterialItem(id="paper", name="Paper", search_query="origami paper")]
        cart = ShoppingCartProposal(cart_id="paper-cart", checkout_url="https://shop.example/checkout")
        film_ready, release_cart = asyncio.Event(), asyncio.Event()

        async def prepare_cart(*, materials):
            self.assertEqual(materials, self.plan.materials)
            await release_cart.wait()
            return cart

        store = await self.enterAsyncContext(open_job_store(self.folder / "guide.sqlite"))
        await store.enqueue(self.project)
        self.project = await store.claim("worker")

        checkpoint = store.checkpoint

        async def persist(worker_id, state, event, data):
            saved = await checkpoint(worker_id, state, event, data)
            if event == "full_video_ready":
                film_ready.set()
            return saved

        with (patch.object(pipeline, "build_shopping_cart_proposal", side_effect=prepare_cart),
              patch.object(store, "checkpoint", side_effect=persist)):
            run = asyncio.create_task(cloud_worker.render(store, "worker", self.project, self.settings, hosted=False))
            try:
                await asyncio.wait_for(film_ready.wait(), 5)
                self.assertFalse(self.project.status.terminal)
                saved, _ = await store.read(self.project.project_id)
                self.assertTrue(saved.full_video_url)
                self.assertTrue(all(step.shot_reviews[-1].status == "passed" for step in saved.media.values()))
            finally:
                release_cart.set()
                await run
        saved, _ = await store.read(self.project.project_id)
        self.assertEqual((saved.cart, saved.status), (cart, ProjectStatus.COMPLETED))
        events = await store.events_after(saved.project_id, 0)
        self.assertEqual([event["event"] for event in events[-3:]], ["full_video_ready", "cart_ready", "status"])

    async def test_complete_plan_streams_before_clips_and_full_video_survives_reload(self):
        result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.COMPLETED)
        updates = [event for event, _ in self.events]
        self.assertLess(updates.index("plan_ready"), updates.index("step_media_ready"))
        self.assertEqual(updates.count("step_media_ready"), 3)
        self.assertEqual(updates[-2:], ["full_video_ready", "status"])
        self.assertEqual(self.references, [
            None, self.folder / "craft-1/1-motion-end.jpg", self.folder / "craft-1/2-motion-end.jpg",
        ])
        self.frames.assert_not_awaited()
        reloaded = ProjectState.model_validate_json(result.model_dump_json())
        self.assertEqual(reloaded.media[2].keyframe_url, "/media/craft-1/2-motion.jpg")
        self.assertEqual(reloaded.full_video_url, "/media/craft-1/full_pov_tutorial.mp4")
        self.assertEqual(reloaded.full_captions_url, "/media/craft-1/full_pov_tutorial.vtt")
        self.assertEqual([a.chapter_start_seconds for a in reloaded.media.values()], [0, 25/24, 51/24])
        history = {entry.id: entry for entry in reloaded.activity}
        for step in self.plan.steps:
            number = step.step_number
            self.assertEqual(history[f"motion-{number}"].model, self.settings.pov_video_model)
            self.assertEqual(history[f"narration-{number}"].model, self.settings.audio_model)
            self.assertEqual(history[f"assembly-{number}"].agent, "FFmpeg")
        self.assertEqual(history["assembly-film"].state, "completed")
        self.assertTrue(all(entry.state == "completed" for entry in history.values()))
        self.assertEqual(self.events[-2][1]["media"], reloaded.model_dump(mode="json")["media"])
        self.assertEqual(self.events[-2][1]["captions_url"], reloaded.full_captions_url)
        captions = (self.folder / reloaded.full_captions_url.removeprefix("/media/")).read_text()
        self.assertIn("00:00:01.042 --> 00:00:01.542\nFold the corners inward", captions)
        self.assertIn("00:00:02.125 --> 00:00:02.625\nOpen the wings", captions)
        chapter = (self.folder / reloaded.media[2].captions_url.removeprefix("/media/")).read_text()
        self.assertEqual(chapter, "WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nFold the corners inward\n\n")

    async def test_next_shot_runs_during_prior_assembly_only_after_real_reference(self):
        self.client.return_value.settings = self.settings.model_copy(update={"pov_video_model": "configured-omni"})
        second_started = asyncio.Event()
        second_voice_ready = asyncio.Event()
        first_assembly_done = asyncio.Event()
        motion, voice, compose = self.motion.side_effect, self.voice.side_effect, self.compose.side_effect

        async def record(**args):
            path = await voice(**args)
            if args["script"] == self.plan.steps[1].narration_script:
                second_voice_ready.set()
            return path

        async def film(**args):
            if args["step"].step_number == 1:
                await asyncio.wait_for(second_voice_ready.wait(), 2)
            elif args["step"].step_number == 2:
                self.assertEqual(args["prev_step_end_frame"].name, "1-motion-end.jpg")
                self.assertEqual(args["speech_seconds"], 0.5)
                self.assertFalse(first_assembly_done.is_set())
                second_started.set()
            return await motion(**args)

        async def assemble(**args):
            if args["video"].video.name == "1-motion.mp4":
                await asyncio.wait_for(second_started.wait(), 2)
                first_assembly_done.set()
            return await compose(**args)

        self.voice.side_effect, self.motion.side_effect, self.compose.side_effect = record, film, assemble
        result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.COMPLETED)
        progress = [data["media_progress"] for event, data in self.events if "media_progress" in data]
        counts = [p["ready_count"] for p in progress]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual((counts[0], counts[-1]), (0, 3))
        for number in (1, 2, 3):
            self.assertEqual([p["stage"] for p in progress if p["step_number"] == number], ["footage", "assembly"])
        self.assertEqual(progress[-1]["stage"], "stitching")
        self.assertEqual(progress[-1]["elapsed_seconds"], result.media_progress.elapsed_seconds)
        self.assertTrue(all(asset.video_model == "configured-omni" for asset in result.media.values()))

    async def test_independent_chapter_plays_while_first_is_rendering_and_film_uses_plan_order(self):
        self.plan.steps[1].visual_parent = 0
        self.plan.steps[2].visual_parent = 1
        second_ready = asyncio.Event()
        references = {}
        motion = self.motion.side_effect

        async def film(**args):
            number = args["step"].step_number
            references[number] = args["prev_step_end_frame"]
            if number == 1:
                await asyncio.wait_for(second_ready.wait(), 2)
            return await motion(**args)

        async def publish(project_id, event, data):
            await self.capture(project_id, event, data)
            if event == "step_media_ready" and data["step_number"] == 2:
                self.assertEqual([self.project.media[i].status for i in (1, 2, 3)], ["generating", "ready", "pending"])
                second_ready.set()

        self.motion.side_effect = film
        result = await pipeline.run_project_pipeline(self.project, publish_event=publish)
        self.assertEqual(result.status, ProjectStatus.COMPLETED, result.error)
        self.assertEqual(references, {1: None, 2: None, 3: self.folder / "craft-1/1-motion-end.jpg"})
        started = [data for event, data in self.events if event == "step_media_started"]
        self.assertEqual([a["step_number"] for a in started], [1, 2, 3])
        self.assertTrue(all(a["status"] == "generating" and a["video_url"] is None for a in started))
        ready = [data["step_number"] for event, data in self.events if event == "step_media_ready"]
        self.assertEqual(ready[0], 2)
        self.assertEqual([result.media[i].chapter_start_seconds for i in (1, 2, 3)], [0, 25/24, 51/24])
        captions = (self.folder / "craft-1/full_pov_tutorial.vtt").read_text()
        self.assertEqual([captions.index(step.narration_script) for step in self.plan.steps],
                         sorted(captions.index(step.narration_script) for step in self.plan.steps))
        self.stitch.assert_awaited_once()

    async def test_failed_parent_never_supplies_an_unrelated_independent_frame(self):
        self.plan.steps[1].visual_parent = 0
        self.plan.steps[2].visual_parent = 1
        second_finished = asyncio.Event()
        motion = self.motion.side_effect

        async def film(**args):
            number = args["step"].step_number
            if number == 1:
                await asyncio.wait_for(second_finished.wait(), 2)
                raise RuntimeError("No usable footage")
            shot = await motion(**args)
            if number == 2:
                second_finished.set()
            return shot

        self.motion.side_effect = film
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.PARTIAL)
        self.assertEqual([result.media[i].status for i in (1, 2, 3)], ["error", "ready", "ready"])
        self.assertEqual(self.references, [None, None])
        self.stitch.assert_not_awaited()

    async def test_visual_reviews_survive_reload_withheld_chapter_cannot_seed_another(self):
        self.plan.steps[1].visual_parent = 0
        self.plan.steps[2].visual_parent = 1
        motion = self.motion.side_effect
        references = {}
        async def film(**args):
            step = args["step"]
            references[step.step_number] = args["prev_step_end_frame"]
            shot = await motion(**args)
            if step.step_number == 1:
                rejected = shot.reviews[0].model_copy(update={"status": "rejected"})
                raise pipeline.ShotRejected([rejected, rejected.model_copy(update={"attempt": 2})])
            if step.step_number == 2:
                unavailable = shot.reviews[0].model_copy(update={"status": "unavailable"})
                return pipeline.MotionShot(shot.video, shot.frames, shot.end_frame, (unavailable,))
            return shot

        self.motion.side_effect = film
        self.frames.side_effect = self.recovery_frames
        self.client.return_value.generate_structured = AsyncMock(side_effect=TimeoutError)
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        saved = ProjectState.model_validate_json(result.model_dump_json())
        self.assertEqual(saved.status, ProjectStatus.PARTIAL)
        self.assertEqual(references, {1: None, 2: None, 3: None})
        self.assertEqual([r.status for r in saved.media[1].shot_reviews], ["rejected", "rejected"])
        self.assertIn("withheld", saved.media[1].error)
        self.assertEqual(saved.media[1].illustration_review.status, "unavailable")
        self.assertIn("visual review was unavailable", saved.media[1].error)
        self.assertFalse(list((self.folder / "craft-1").glob("*_frame_*.png")))
        self.assertIsNone(saved.media[1].video_url)
        self.assertEqual(saved.media[2].shot_reviews[-1].status, "unavailable")
        self.assertEqual(saved.media[2].status, "ready")
        self.stitch.assert_not_awaited()

    async def test_serial_visuals_complete_in_guide_order_and_only_share_declared_parent_frames(self):
        self.settings.max_parallel_shots = 1
        self.plan.steps[2].visual_parent = 0
        result = await self.run_project()
        order = [data["step_number"] for event, data in self.events if event == "step_media_started"]
        self.assertEqual(result.status, ProjectStatus.COMPLETED)
        self.assertEqual(order, [1, 2, 3])
        self.assertEqual(self.references, [None, self.folder / "craft-1/1-motion-end.jpg", None])

    async def test_visual_request_limit_applies_to_independent_shots(self):
        for step in self.plan.steps:
            step.visual_parent = 0
        motion = self.motion.side_effect
        active = peak = 0

        async def film(**args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)
                return await motion(**args)
            finally:
                active -= 1

        self.motion.side_effect = film
        for limit in (1, 3):
            with self.subTest(limit=limit):
                self.settings.max_parallel_shots = limit
                peak = 0
                result = await self.run_project()
                self.assertEqual(result.status, ProjectStatus.COMPLETED)
                self.assertEqual(peak, limit)

    async def test_cancellation_joins_active_providers_and_does_not_start_queued_chapters(self):
        for step in self.plan.steps:
            step.visual_parent = 0
        self.project.plan = self.plan
        all_started = asyncio.Event()
        active = set()

        async def slow_job(key):
            active.add(key)
            if len(active) == 4:
                all_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active.remove(key)

        async def film(**args):
            return await slow_job(("footage", args["step"].step_number))
        async def speak(**args):
            return await slow_job(args["script"])
        self.motion.side_effect, self.voice.side_effect = film, speak
        job = asyncio.create_task(pipeline.generate_media_for_plan(self.project, self.client.return_value, self.capture))
        await asyncio.wait_for(all_started.wait(), 2)
        self.assertEqual({key for key in active if isinstance(key, tuple)}, {("footage", 1), ("footage", 2)})
        self.assertEqual(self.project.media[3].status, "pending")
        self.assertEqual([data["step_number"] for event, data in self.events if event == "step_media_started"], [1, 2])
        self.assertEqual({entry.id for entry in self.project.activity if entry.state == "started"},
                         {"motion-1", "motion-2", "narration-1", "narration-2"})
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertFalse(active)
        self.assertEqual(self.motion.await_count, 2)
        self.assertEqual(self.voice.await_count, 2)
        self.stitch.assert_not_awaited()

    async def test_partial_failure_preserves_other_clips_and_avoids_skipping_a_step_in_full_video(self):
        original = self.motion.side_effect

        async def fail_middle(**kwargs):
            if kwargs["step"].step_number == 2:
                raise TimeoutError()
            return await original(**kwargs)

        self.motion.side_effect = fail_middle
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.PARTIAL)
        self.assertEqual([asset.status for asset in result.media.values()], ["ready", "error", "ready"])
        self.assertEqual(self.references, [None, None])
        self.assertEqual(next(e.state for e in result.activity if e.id == "motion-2"), "failed")
        self.assertFalse(any(e.id == "assembly-2" for e in result.activity))
        self.assertIsNone(result.full_video_url)
        self.assertIsNone(result.full_captions_url)
        self.assertTrue(all(a.chapter_start_seconds is None for a in result.media.values()))
        self.frames.assert_awaited_once()
        error = next(data for event, data in self.events if event == "step_media_error")
        self.assertEqual(error["motion_failure"], "Motion footage timed out.")
        self.assertIn("Chapter illustrations could not be generated", error["error"])
        self.assertTrue(error["audio_url"].endswith("step_02_voice.wav"))
        self.stitch.assert_not_awaited()

    async def test_rejected_motion_uses_only_reviewed_illustrations_and_preserves_history_on_reuse(self):
        motion = self.motion.side_effect
        wrong = FrameFinding(verdict="contradicts", sample=1, observation="The paper remains unfolded.")

        async def film(**kwargs):
            shot = await motion(**kwargs)
            if kwargs["step"].step_number == 2:
                raise pipeline.ShotRejected([shot.reviews[0].model_copy(update={"status": "rejected", "result": wrong})])
            return shot

        review_inputs = []
        def inspect(*, contents, response_schema, **kwargs):
            review_inputs.append(contents)
            if len(review_inputs) == 1:
                raise TimeoutError()
            return response_schema.model_validate({
                name: {"verdict": "matches", "sample": n, "observation": f"Visible {name} on this frame."}
                for n, name in enumerate(("opening", "contact", "result"), 1)
            })

        async def assemble(*, out_mp4, frames, **kwargs):
            self.assertEqual([f.read_bytes() for f in frames], list(PNG_FRAMES))
            out_mp4.touch()
            return out_mp4

        self.motion.side_effect = film
        self.frames.side_effect = self.recovery_frames
        self.client.return_value.generate_structured = AsyncMock(side_effect=inspect)
        with patch.object(pipeline, "compose_step_pov_video", side_effect=assemble):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.COMPLETED, result.error)
        self.assertEqual([a.media_mode for a in result.media.values()], ["motion", "illustrated_frames", "motion"])
        saved = ProjectState.model_validate_json(result.model_dump_json())
        asset = saved.media[2]
        self.assertEqual(asset.illustration_review.status, "passed")
        self.assertEqual(asset.illustration_review.source_segment_ids, [])
        self.assertEqual(asset.shot_reviews[0].result, wrong)
        self.assertIn(wrong.observation, asset.motion_failure)
        self.assertEqual(asset.keyframe_url, "/media/craft-1/step_02_frame_2.png")
        self.assertEqual(self.references[-1], self.folder / "craft-1/step_02_frame_3.png")
        self.assertEqual(self.frames.await_args.kwargs["prev_step_end_frame"], self.folder / "craft-1/1-motion-end.jpg")
        self.assertEqual(self.frames.await_args.kwargs["correction"], wrong.observation)
        self.assertEqual(len(review_inputs), 2)
        self.assertEqual(review_inputs[0], review_inputs[1])
        self.assertEqual(next(e.state for e in result.activity if e.id == "motion-2"), "failed")
        self.assertEqual(next(e.state for e in result.activity if e.id == "illustration-2"), "completed")
        self.assertEqual([data["status"] for event, data in self.events
                          if event == "step_media_ready" and data["step_number"] == 2], ["ready"])
        self.assertNotIn("step_media_error", [event for event, _ in self.events])
        await pipeline.generate_media_for_plan(saved, self.client.return_value, max_steps=1)
        self.assertTrue(saved.full_video_url)
        self.assertEqual(saved.media[2], asset)
        self.assertEqual(saved.media[2].media_mode, "illustrated_frames")
        self.frames.assert_awaited_once()

    async def test_no_voice_keeps_written_plan_and_never_claims_complete(self):
        self.voice.side_effect = RuntimeError("provider failed")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.FAILED)
        self.assertEqual(result.plan, self.plan)
        self.assertIn("0 of 3", result.status_message)
        self.assertTrue(all("narration" in asset.error for asset in result.media.values()))
        self.assertEqual(self.events[-1][1]["status"], "failed")

    async def test_stitch_failure_releases_every_individual_clip(self):
        self.stitch.side_effect = OSError("assembly failed")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.PARTIAL)
        self.assertTrue(all(asset.video_url for asset in result.media.values()))
        self.assertIsNone(result.full_video_url)
        self.assertIsNone(result.full_captions_url)

    async def test_limited_render_and_saved_rerender_publish_only_available_clips(self):
        result = await self.run_project(max_media_steps=1)
        self.assertEqual(result.status, ProjectStatus.COMPLETED)
        self.assertEqual([a.status for a in result.media.values()], ["ready", "skipped", "skipped"])
        self.stitch.assert_not_awaited()
        await pipeline.generate_media_for_plan(result, self.client.return_value)
        await pipeline.generate_media_for_plan(result, self.client.return_value, max_steps=1)
        self.assertTrue(result.full_video_url)
        self.assertTrue(all(a.status == "ready" for a in result.media.values()))
        (self.folder / "craft-1/step_02_pov.mp4").unlink()
        await pipeline.generate_media_for_plan(result, self.client.return_value, max_steps=1)
        self.assertIsNone(result.full_video_url)
        self.assertEqual(result.media[2].status, "skipped")
        self.motion.side_effect = RuntimeError("No replacement footage")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            await pipeline.generate_media_for_plan(result, self.client.return_value, max_steps=1)
        self.assertEqual(result.status, ProjectStatus.PARTIAL)
        self.assertEqual([asset.status for asset in result.media.values()], ["error", "skipped", "ready"])

    async def test_explicit_illustration_and_legacy_chapters_keep_their_original_mode(self):
        self.settings.pov_media_mode = "illustrated_frames"
        async def frames(*, step, bible, out_dir, gemini, prev_step_end_frame):
            return [out_dir / f"{step.step_number}-start.png", out_dir / f"{step.step_number}-end.png"]
        self.frames.side_effect = frames
        async def compose(*, frames, voice_wav, out_mp4, min_duration):
            out_mp4.touch()
            return out_mp4
        with patch.object(pipeline, "compose_step_pov_video", side_effect=compose):
            result = await self.run_project()
            # Old saved guides can contain the narrated MP4 without timing fields or a separate WAV.
            old = result.model_dump()
            for record in old["media"].values():
                for name in ("media_mode", "duration_seconds", "chapter_start_seconds", "captions_url"):
                    record.pop(name)
            old.pop("full_captions_url")
            result = ProjectState.model_validate(old)
            (self.folder / "craft-1/step_02_voice.wav").unlink()
            await pipeline.generate_media_for_plan(result, self.client.return_value, max_steps=1)
        self.motion.assert_not_awaited()
        self.assertTrue(result.full_video_url)
        self.assertIsNone(result.media[2].audio_url)
        self.assertEqual(result.media[2].chapter_start_seconds, 25/24)
        self.assertEqual(result.media[2].media_mode, "illustrated_frames")
        self.assertEqual(result.media[2].keyframe_url, "/media/craft-1/2-start.png")
        self.settings.pov_media_mode = "motion"
        await pipeline.generate_media_for_plan(result, self.client.return_value, max_steps=1)
        self.assertIsNone(result.full_video_url)
        self.assertEqual([a.status for a in result.media.values()], ["ready", "skipped", "skipped"])

    async def test_plan_only_never_enqueues_media(self):
        self.project.full_video_url = "/media/craft-1/previous.mp4"
        self.project.full_captions_url = "/media/craft-1/previous.vtt"
        result = await self.run_project(generate_media=False)
        self.assertEqual(result.status, ProjectStatus.COMPLETED)
        self.assertIsNone(result.full_video_url)
        self.assertIsNone(result.full_captions_url)
        self.assertEqual({asset.status for asset in result.media.values()}, {"skipped"})
        self.frames.assert_not_awaited()
        self.motion.assert_not_awaited()

    async def test_failed_research_exposes_only_actionable_message(self):
        self.research.side_effect = RuntimeError("provider request details")
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.FAILED)
        self.assertNotIn("provider request details", result.error)
        self.assertEqual([event for event, _ in self.events][-2:], ["error", "status"])
        self.planner.assert_not_awaited()
