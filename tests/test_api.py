import asyncio
import json
import os
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from nyc_gemini4_hack import blob_media, cloud_worker
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.models import AgentActivity, ProjectState, ProjectStatus, StepMediaAsset
from nyc_gemini4_hack.server import create_app
from tests.test_pipeline import paper_plan


class ApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(_env_file=None, gemini_api_key="test", youtube_api_key="test",
                                 sqlite_path=root / "data" / "jobs.sqlite3", media_output_dir=root / "media")

    async def make_guide(self, project, publish_event):
        project.plan = paper_plan()
        await publish_event(project.project_id, "plan_ready", project.plan.model_dump(mode="json"))
        directory = self.settings.media_output_dir / project.project_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "guide.mp4").write_bytes(b"fixture video")
        project.full_video_url = f"/media/{project.project_id}/guide.mp4"
        project.media[1] = StepMediaAsset(step_number=1, status="ready", video_url=project.full_video_url)
        project.status = ProjectStatus.COMPLETED
        project.status_message = "Your guide is ready."
        await publish_event(project.project_id, "status", {"status": "completed", "message": project.status_message})
        return project

    def test_created_project_media_and_snapshot_survive_restart(self):
        with patch.object(cloud_worker, "run_project_pipeline", side_effect=self.make_guide):
            with TestClient(create_app(settings=self.settings)) as client:
                created = client.post("/api/projects", data={"goal": " Fold a glider ", "owned_items": " paper, , ruler "})
                self.assertEqual(created.status_code, 202)
                data = created.json()
                client.get(data["events_url"])  # Wait for completion or attach after the final event.
        with TestClient(create_app(settings=self.settings)) as client:
            state = client.get(f"/api/projects/{data['project_id']}").json()
            self.assertEqual((state["goal"], state["owned_items"], state["status"]),
                             ("Fold a glider", ["paper", "ruler"], "completed"))
            stream = client.get(data["events_url"])
            self.assertTrue(stream.headers["content-type"].startswith("text/event-stream"))
            events = stream.text.strip().split("\n\n")
            self.assertEqual([e.splitlines()[0] for e in events], ["event: snapshot", "event: status"])
            self.assertEqual(json.loads(events[0].split("\ndata: ", 1)[1]), state)
            media = client.get(state["full_video_url"], headers={"Range": "bytes=0-3"})
            self.assertEqual((media.status_code, media.content), (206, b"fixt"))

    def test_input_and_unavailable_actions(self):
        with TestClient(create_app(settings=self.settings)) as client, patch.object(
            cloud_worker, "run_project_pipeline", side_effect=self.make_guide
        ):
            self.assertEqual(client.post("/api/projects", data={"goal": "   "}).status_code, 422)
            for content, code in [(b"plain", 415), (b"0" * (8 * 1024 * 1024 + 1), 413)]:
                result = client.post("/api/projects", data={"goal": "Fold paper"},
                                     files={"desk_photo": ("desk.jpg", content, "image/jpeg")})
                self.assertEqual(result.status_code, code)
            self.assertFalse(self.settings.media_output_dir.exists())
            health = client.get("/api/health").json()
            self.assertTrue(health["generation_ready"])
            self.assertFalse(health["capabilities"]["commerce"])
            self.assertEqual(client.get("/api/projects/unknown/events").status_code, 404)
            created = client.post("/api/projects", data={"goal": "Fold a glider"}).json()
            path = f"/api/projects/{created['project_id']}"
            client.get(created["events_url"])
            rebuilt = client.post(path + "/cart/rebuild", json={})
            self.assertEqual(rebuilt.status_code, 200)
            self.assertEqual(rebuilt.json()["matched_items"], [])
            self.assertEqual(client.post(path + "/steps/1/checkin", files={"frame": ("f.jpg", b"f")}).status_code, 501)

    def test_local_media_from_an_earlier_cli_run(self):
        folder = self.settings.media_output_dir / "saved-run"
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(b"previous movie")
        with TestClient(create_app(settings=self.settings)) as client:
            self.assertEqual(client.get("/media/saved-run/clip.mp4").content, b"previous movie")

    def test_legacy_narration_is_recovered_only_in_local_playback_snapshots(self):
        settings = self.settings.model_copy(update={"gemini_api_key": ""})
        project = ProjectState(project_id="saved-guide", goal="Fold paper", status=ProjectStatus.PARTIAL)
        directory = settings.media_output_dir / project.project_id
        directory.mkdir(parents=True)
        recording = directory / "step_01_voice.wav"
        with wave.open(str(recording), "wb") as wav:
            wav.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            wav.writeframes(bytes(480))
        for number, state in enumerate(("completed", "completed", "failed", "completed"), 1):
            project.media[number] = StepMediaAsset(step_number=number, status="error")
            project.activity.append(AgentActivity(
                id=f"narration-{number}", stage="narration", state=state, step_number=number,
                label="Chapter narration", agent="Speech generator",
            ))
        (directory / "step_02_voice.wav").write_bytes(recording.read_bytes()[:-2])
        (directory / "step_03_voice.wav").write_bytes(recording.read_bytes())

        async def save():
            async with open_job_store(settings.sqlite_path) as store:
                await store.enqueue(project)

        asyncio.run(save())
        path = f"/api/projects/{project.project_id}"
        with TestClient(create_app(settings=settings)) as client:
            snapshot = client.get(path).json()
            record = client.get(path + "/events").text.split("\n\n", 1)[0]
            self.assertEqual(record.splitlines()[0], "event: snapshot")
            self.assertEqual(json.loads(record.split("\ndata: ", 1)[1]), snapshot)
            url = f"/media/{project.project_id}/{recording.name}"
            self.assertEqual([snapshot["media"][str(i)]["audio_url"] for i in range(1, 5)], [url, None, None, None])
            self.assertEqual(client.get(url).content, recording.read_bytes())

        async def read_saved():
            async with open_job_store(settings.sqlite_path) as store:
                return await store.read(project.project_id)

        self.assertEqual(asyncio.run(read_saved()), (project, 0))
        # Use the same saved project and files to demonstrate the hosted view only exposes published assets.
        with (patch.dict(os.environ, {"TURSO_DATABASE_URL": "libsql://test.invalid", "TURSO_AUTH_TOKEN": "test"}),
              patch("nyc_gemini4_hack.server.open_job_store", lambda _: open_job_store(settings.sqlite_path)),
              TestClient(create_app(hosted=True, settings=settings)) as hosted):
            self.assertIsNone(hosted.get(path).json()["media"]["1"]["audio_url"])

    def test_hosted_api_worker_and_second_api_share_the_project(self):
        # Each app entrypoint and the worker opens its own store connection.
        settings = self.settings

        async def render_externally():
            async with open_job_store(settings.sqlite_path) as store:
                project = await store.claim("renderer")
                self.assertIsNotNone(project)
                await cloud_worker.render(store, "renderer", project, settings, hosted=True)

        async def register():
            async with open_job_store(settings.sqlite_path) as store:
                await store.heartbeat("renderer")

        asyncio.run(register())
        with ExitStack() as patches:
            patches.enter_context(patch.dict(os.environ, {"TURSO_DATABASE_URL": "libsql://test.invalid",
                                                         "TURSO_AUTH_TOKEN": "test", "BLOB_READ_WRITE_TOKEN": "test"}))
            patches.enter_context(patch("nyc_gemini4_hack.server.open_job_store", lambda _: open_job_store(settings.sqlite_path)))
            patches.enter_context(patch.object(cloud_worker, "run_project_pipeline", side_effect=self.make_guide))
            uploaded = patches.enter_context(patch.object(blob_media, "upload", new_callable=AsyncMock))
            signed = patches.enter_context(patch.object(blob_media, "delivery_url", new_callable=AsyncMock,
                                                       return_value="https://test.public.blob.vercel-storage.com/guide.mp4"))
            with TestClient(create_app(hosted=True, settings=settings)) as api_one:
                self.assertTrue(api_one.get("/api/health").json()["generation_ready"])
                created = api_one.post("/api/projects", data={"goal": "Fold a glider"},
                                       files={"desk_photo": ("photo.png", b"\x89PNG\r\n\x1a\nimage", "image/png")})
                self.assertEqual(created.status_code, 202)
            project_id = created.json()["project_id"]
            asyncio.run(render_externally())
            self.assertEqual([call.args[1] for call in uploaded.await_args_list], ["desk_photo.png", "guide.mp4"])
            with TestClient(create_app(hosted=True, settings=settings)) as api_two:
                state = api_two.get(f"/api/projects/{project_id}").json()
                self.assertEqual(state["status"], "completed")
                self.assertIn("event: snapshot", api_two.get(created.json()["events_url"]).text)
                path = state["full_video_url"]
                media = api_two.get(path, headers={"Range": "bytes=5-"}, follow_redirects=False)
                self.assertEqual(media.status_code, 307)
                self.assertEqual(media.headers["location"], signed.return_value)
                self.assertEqual(api_two.head(path, follow_redirects=False).status_code, 307)
                self.assertEqual(api_two.get(f"/media/{project_id}/unpublished.mp4").status_code, 404)

    def test_unconfigured_hosted_app_can_still_serve_the_example(self):
        import app as deployment
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            with TestClient(deployment.app) as client:
                self.assertFalse(client.get("/api/health").json()["generation_ready"])
                self.assertEqual(client.post("/api/projects", data={"goal": "Fold a glider"}).status_code, 503)
                if Path("frontend/dist/index.html").exists():
                    self.assertEqual(client.get("/").status_code, 200)
                    example = client.get("/static/example/project.json").json()
                    self.assertEqual(client.get(example["full_video_url"], headers={"Range": "bytes=0-1"}).status_code, 206)

    def test_concurrent_chapters_publish_consistent_snapshots_and_upload_once(self):
        async def check(hosted):
            project = ProjectState(project_id=f"parallel-{int(hosted)}", goal="Fold two sheets")
            saving, release = asyncio.Event(), asyncio.Event()
            snapshots = []
            uploads = []
            async with open_job_store(self.settings.sqlite_path) as store:
                await store.enqueue(project)
                project = await store.claim("worker")
                persist = store.checkpoint

                async def save(worker, state, event, data):
                    if not hosted and not snapshots:
                        saving.set()
                        await release.wait()
                    snapshots.append((event, list(state.media)))
                    return await persist(worker, state, event, data)

                async def upload(_, name, __):
                    if name == "second.mp4":
                        saving.set()
                        await release.wait()
                    uploads.append(name)

                async def two_chapters(state, publish_event):
                    async def ready(number, name):
                        clip = StepMediaAsset(step_number=number, status="ready",
                                              video_url=f"/media/{state.project_id}/{name}.mp4")
                        state.media[number] = clip
                        await publish_event(state.project_id, "step_media_ready", clip.model_dump())

                    second = asyncio.create_task(ready(2, "second"))
                    await saving.wait()
                    first = asyncio.create_task(ready(1, "first"))
                    await asyncio.sleep(0)
                    release.set()
                    await asyncio.gather(second, first)
                    state.status = ProjectStatus.COMPLETED
                    await publish_event(state.project_id, "status", {"status": "completed"})

                with patch.object(store, "checkpoint", save), patch.object(blob_media, "upload", upload), \
                        patch.object(cloud_worker, "run_project_pipeline", two_chapters):
                    await asyncio.wait_for(cloud_worker.render(store, "worker", project, self.settings, hosted=hosted), 5)
                self.assertEqual(snapshots, [("step_media_ready", [2]), ("step_media_ready", [2, 1]), ("status", [2, 1])])
                self.assertEqual(uploads, ["second.mp4", "first.mp4"] if hosted else [])
                events = await store.events_after(project.project_id, 0)
                self.assertEqual([e["data"].get("step_number") for e in events], [2, 1, None])

        for hosted in (False, True):
            with self.subTest(hosted=hosted):
                asyncio.run(check(hosted))

    def test_live_stream_replays_updates_written_after_opening_snapshot(self):
        from threading import Event

        allow_render = Event()

        async def delayed(project, publish_event):
            await asyncio.to_thread(allow_render.wait, 3)
            return await self.make_guide(project, publish_event)

        from nyc_gemini4_hack import server
        format_event = server.sse

        def signal_open(event, payload):
            if event == "snapshot":
                allow_render.set()
            return format_event(event, payload)

        with patch.object(cloud_worker, "run_project_pipeline", side_effect=delayed), patch.object(server, "sse", signal_open):
            with TestClient(create_app(settings=self.settings)) as client:
                created = client.post("/api/projects", data={"goal": "Fold a glider"}).json()
                response = client.get(created["events_url"])
                records = [item.splitlines()[0] for item in response.text.strip().split("\n\n")]
                self.assertEqual(records, ["event: snapshot", "event: status", "event: plan_ready", "event: status"])
                self.assertIn('"status": "completed"', response.text)
