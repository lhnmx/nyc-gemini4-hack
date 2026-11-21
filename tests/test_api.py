import asyncio
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from nyc_gemini4_hack import blob_media, cloud_worker
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.models import ProjectStatus, StepMediaAsset
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
