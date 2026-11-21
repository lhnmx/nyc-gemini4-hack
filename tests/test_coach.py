import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from nyc_gemini4_hack.agents.coach import build_coach_system_instruction, create_live_coach_session
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.models import ProjectStatus
from nyc_gemini4_hack.server import create_app
from tests.test_pipeline import paper_plan


class CoachTests(unittest.IsolatedAsyncioTestCase):
    def test_system_instruction_grounds_steps_and_quiet_behavior(self):
        plan = paper_plan()
        instruction = build_coach_system_instruction(plan, current_step=1)
        self.assertIn(plan.project_title, instruction)
        self.assertIn("Step 1:", instruction)
        self.assertIn("DO NOT BE CHATTY", instruction)
        self.assertIn("flag_mistake", instruction)
        self.assertIn("complete_step", instruction)
        self.assertIn("sync_current_step", instruction)

    async def test_create_live_coach_session_mints_ephemeral_token_url(self):
        settings = Settings(_env_file=None, gemini_api_key="test-key")
        fake_client = SimpleNamespace(
            aio=SimpleNamespace(
                auth_tokens=SimpleNamespace(
                    create=AsyncMock(return_value=SimpleNamespace(name="auth_tokens/abc123"))
                ),
                aclose=AsyncMock(),
            ),
            close=lambda: None,
        )
        with patch("nyc_gemini4_hack.agents.coach.genai.Client", return_value=fake_client):
            session = await create_live_coach_session(paper_plan(), settings, current_step=1)
        self.assertIn("BidiGenerateContentConstrained?access_token=auth_tokens%2Fabc123", session.ws_url)
        self.assertEqual(session.model, "models/gemini-3.8-live")
        self.assertEqual(session.initial_step, 1)
        self.assertEqual(len(session.tools[0]["functionDeclarations"]), 3)


class CoachApiTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = Settings(
            _env_file=None,
            gemini_api_key="test-key",
            youtube_api_key="test-yt",
            sqlite_path=root / "data" / "jobs.sqlite3",
            media_output_dir=root / "media",
        )

    def test_coach_session_endpoint_for_example_and_missing_key(self):
        fake_session = {
            "ws_url": "wss://example.test/ws?access_token=tok",
            "model": "models/gemini-3.8-live",
            "voice": "Aoede",
            "system_instruction": "Coach prompt",
            "tools": [],
            "initial_step": 1,
        }
        with patch("nyc_gemini4_hack.server.create_live_coach_session", new=AsyncMock(return_value=fake_session)):
            with TestClient(create_app(settings=self.settings)) as client:
                health = client.get("/api/health").json()
                self.assertTrue(health["capabilities"]["live_coach"])
                resp = client.post(
                    "/api/projects/example/coach/session",
                    json={"current_step": 1, "plan": paper_plan().model_dump(mode="json")},
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["ws_url"], fake_session["ws_url"])
                self.assertEqual(client.post("/api/projects/does-not-exist/coach/session", json={}).status_code, 404)

        no_key = Settings(_env_file=None, gemini_api_key="", youtube_api_key="")
        with TestClient(create_app(settings=no_key)) as client:
            resp = client.post("/api/projects/example/coach/session", json={})
            self.assertEqual(resp.status_code, 503)

    def test_coach_session_endpoint_for_stored_project(self):
        async def make_plan(project, publish_event, **options):
            project.plan = paper_plan()
            project.status = ProjectStatus.COMPLETED
            project.status_message = "Done"
            await publish_event(project.project_id, "status", {"status": "completed", "message": "Done"})
            return project

        fake_session = {
            "ws_url": "wss://example.test/ws?access_token=tok2",
            "model": "models/gemini-3.8-live",
            "voice": "Aoede",
            "system_instruction": "Coach prompt",
            "tools": [],
            "initial_step": 1,
        }
        with (
            patch("nyc_gemini4_hack.cloud_worker.run_project_pipeline", side_effect=make_plan),
            patch("nyc_gemini4_hack.server.create_live_coach_session", new=AsyncMock(return_value=fake_session)),
            TestClient(create_app(settings=self.settings)) as client,
        ):
            created = client.post("/api/projects", data={"goal": "Fold a glider"}).json()
            client.get(created["events_url"])
            resp = client.post(f"/api/projects/{created['project_id']}/coach/session", json={"current_step": 1})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["ws_url"], fake_session["ws_url"])

    def test_coach_inspect_endpoint(self):
        fake_inspect = {
            "step_number": 1,
            "verdict": "ADJUST",
            "detected_step_number": 1,
            "what_i_see": "The top corners do not meet at the center crease.",
            "mistakes": ["Top corners are uneven by about 1 cm."],
            "fix": "Unfold the top corner and align its edge flush with the center crease.",
            "next_action": "Press the crease flat before moving to Step 2.",
        }
        with patch("nyc_gemini4_hack.server.inspect_workpiece_frame", new=AsyncMock(return_value=fake_inspect)):
            with TestClient(create_app(settings=self.settings)) as client:
                resp = client.post(
                    "/api/projects/example/coach/inspect",
                    data={"step_number": "1", "user_question": "Is this right?", "plan_json": paper_plan().model_dump_json()},
                    files={"frame": ("frame.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg")},
                )
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["verdict"], "ADJUST")
                self.assertIn("center crease", body["fix"])
