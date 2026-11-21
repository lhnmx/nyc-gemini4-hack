import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from nyc_gemini4_hack import pipeline
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.models import BuildPlan, ProjectState, ProjectStatus
from nyc_gemini4_hack.media import composer


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
        self.settings = Settings(_env_file=None, media_output_dir=self.folder)
        self.enterContext(patch.object(pipeline, "get_settings", return_value=self.settings))
        self.project = ProjectState(project_id="craft-1", goal="Fold a paper glider")
        self.plan = paper_plan()
        self.client = self.enterContext(patch.object(pipeline, "Gemini4Client"))
        self.client.return_value.aclose = AsyncMock()
        self.research = self.enterContext(patch.object(
            pipeline, "research_goal_on_youtube", new=AsyncMock(return_value=([], [])),
        ))
        self.planner = self.enterContext(patch.object(
            pipeline, "synthesize_build_plan", new=AsyncMock(return_value=self.plan),
        ))
        self.events = []
        self.references = []

        async def motion(*, step, bible, out_dir, gemini, prev_step_end_frame):
            self.references.append(prev_step_end_frame)
            return out_dir / f"{step.step_number}-motion.mp4"

        async def voice(*, script, out_wav, gemini, prefer_gemini_audio, duration_seconds):
            self.assertTrue(prefer_gemini_audio)
            self.assertEqual(duration_seconds, 7)
            with wave.open(str(out_wav), "wb") as audio:
                audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                audio.writeframes(bytes(8000))  # Half a second of recorded narration
            return out_wav

        async def compose(*, video, voice_wav, out_mp4):
            out_mp4.touch()
            return out_mp4, video.with_suffix(".jpg"), video.with_name(f"{video.stem}-end.jpg")

        async def stitch(clips, output, *, durations):
            self.assertEqual([clip.name for clip in clips], [f"step_{i:02d}_pov.mp4" for i in (1, 2, 3)])
            self.assertEqual(durations, [25/24, 26/24, 27/24])
            self.assertTrue(all(clip.is_file() for clip in clips))
            output.touch()
            return output

        self.motion = self.enterContext(patch.object(pipeline, "generate_step_motion", side_effect=motion))
        self.frames = self.enterContext(patch.object(pipeline, "generate_step_pov_frames"))
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
        self.assertEqual(self.events[-2][1]["media"], reloaded.model_dump(mode="json")["media"])
        self.assertEqual(self.events[-2][1]["captions_url"], reloaded.full_captions_url)
        captions = (self.folder / reloaded.full_captions_url.removeprefix("/media/")).read_text()
        self.assertIn("00:00:01.042 --> 00:00:01.542\nFold the corners inward", captions)
        self.assertIn("00:00:02.125 --> 00:00:02.625\nOpen the wings", captions)
        chapter = (self.folder / reloaded.media[2].captions_url.removeprefix("/media/")).read_text()
        self.assertEqual(chapter, "WEBVTT\n\n00:00:00.000 --> 00:00:00.500\nFold the corners inward\n\n")

    async def test_partial_failure_preserves_other_clips_and_avoids_skipping_a_step_in_full_video(self):
        original = self.motion.side_effect

        async def fail_middle(**kwargs):
            if kwargs["step"].step_number == 2:
                raise RuntimeError("motion unavailable")
            return await original(**kwargs)

        self.motion.side_effect = fail_middle
        with self.assertLogs(pipeline.logger, level="ERROR"):
            result = await self.run_project()
        self.assertEqual(result.status, ProjectStatus.PARTIAL)
        self.assertEqual([asset.status for asset in result.media.values()], ["ready", "error", "ready"])
        self.assertEqual(self.references, [None, None])
        self.assertIsNone(result.full_video_url)
        self.assertIsNone(result.full_captions_url)
        self.assertTrue(all(a.chapter_start_seconds is None for a in result.media.values()))
        self.frames.assert_not_awaited()
        self.stitch.assert_not_awaited()

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
        await pipeline.generate_media_for_plan(result, MagicMock())
        await pipeline.generate_media_for_plan(result, MagicMock(), max_steps=1)
        self.assertTrue(result.full_video_url)
        self.assertTrue(all(a.status == "ready" for a in result.media.values()))
        (self.folder / "craft-1/step_02_pov.mp4").unlink()
        await pipeline.generate_media_for_plan(result, MagicMock(), max_steps=1)
        self.assertIsNone(result.full_video_url)
        self.assertEqual(result.media[2].status, "skipped")

    async def test_illustrations_require_explicit_mode_and_cannot_fill_missing_motion(self):
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
            await pipeline.generate_media_for_plan(result, MagicMock(), max_steps=1)
        self.motion.assert_not_awaited()
        self.assertTrue(result.full_video_url)
        self.assertIsNone(result.media[2].audio_url)
        self.assertEqual(result.media[2].chapter_start_seconds, 25/24)
        self.assertEqual(result.media[2].media_mode, "illustrated_frames")
        self.assertEqual(result.media[2].keyframe_url, "/media/craft-1/2-start.png")
        self.settings.pov_media_mode = "motion"
        await pipeline.generate_media_for_plan(result, MagicMock(), max_steps=1)
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
