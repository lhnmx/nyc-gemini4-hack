import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from nyc_gemini4_hack import pipeline
from nyc_gemini4_hack.agents import researcher
from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.models import DecisionResult, ProjectState, ProjectStatus, ShoppingCartProposal
from tests.test_planner import grounding, make_plan
from tests.test_research import extract


class PipelineActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.project = ProjectState(project_id="paper", goal="Fold paper")
        self.gemini = Gemini4Client(Settings(
            _env_file=None, gemini_model_carbon="configured-video-analyst",
            gemini_model_argon="configured-build-planner", gemini_model_hydrogen="configured-physical-planner",
        ))
        self.enterContext(patch.object(pipeline, "Gemini4Client", return_value=self.gemini))
        self.enterContext(patch.object(pipeline, "build_shopping_cart_proposal", return_value=ShoppingCartProposal()))
        self.videos = [
            researcher.YouTubeCandidate(key, f"https://youtu.be/{key}", key, "Maker", "")
            for key in ("aaaaaaaaaaa", "bbbbbbbbbbb")
        ]
        self.search = self.enterContext(patch.object(
            researcher, "search_youtube_tutorials", AsyncMock(return_value=self.videos)))
        self.gate = self.enterContext(patch.object(self.gemini, "decide_batch", AsyncMock(return_value={
            "video_1_relevance": DecisionResult(gate_name="video_1_relevance", choice="KEEP_TUTORIAL",
                                                model_used="decisions-endpoint", via_decisions_api=True),
            "video_2_relevance": DecisionResult(gate_name="video_2_relevance", choice="SKIP_FILLER",
                                                model_used="fallback-model", via_decisions_api=False),
        })))
        self.plan = make_plan()
        self.provider = self.enterContext(patch.object(
            self.gemini, "generate_structured", AsyncMock(side_effect=[extract(), self.plan, grounding(self.plan, [1, 2])]),
        ))
        self.events = []

    async def run_guide(self, store=None, **options):
        async def capture(project_id, name, data):
            self.assertEqual(project_id, self.project.project_id)
            if name == "activity":
                # The checkpoint persisted by the worker must agree with the event.
                saved = next(op for op in self.project.activity if op.id == data["id"])
                self.assertEqual(saved.model_dump(), data)
            self.events.append((name, data))
            if store is not None:
                self.assertTrue(await store.checkpoint("worker", self.project, name, data))

        return await pipeline.run_project_pipeline(self.project, publish_event=capture, generate_media=False, **options)

    def updates(self, operation_id):
        return [data for name, data in self.events if name == "activity" and data["id"] == operation_id]

    async def test_discovery_and_physical_planning_report_actual_configured_models(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        store = await self.enterAsyncContext(open_job_store(folder / "guide.sqlite3"))
        await store.enqueue(self.project)
        self.project = await store.claim("worker")
        result = await self.run_guide(store=store)
        self.assertEqual(result.status, ProjectStatus.COMPLETED, result.error)
        self.assertEqual(len(result.decisions_log), 2)
        gate_start, gate_done = self.updates("source-selection")
        self.assertFalse(gate_start["model"])
        self.assertNotIn("Decisions API", gate_start["agent"])
        self.assertEqual(gate_done["model"], "decisions-endpoint, fallback-model")
        self.assertIn("Decisions API", gate_done["agent"])
        self.assertIn("Structured fallback", gate_done["agent"])
        restored, revision = await store.read(result.project_id)
        self.assertEqual(revision, len(self.events))
        expected = [
            ("source-search", None), ("source-selection", gate_done["model"]),
            ("analyze-aaaaaaaaaaa", "configured-video-analyst"), ("plan-synthesis", "configured-build-planner"),
            ("physical-planning", "configured-physical-planner"),
        ]
        self.assertEqual([(op.id, op.model) for op in restored.activity], expected)
        for op in restored.activity:
            start, done = self.updates(op.id)
            self.assertEqual([start["state"], done["state"]], ["started", "completed"])
            self.assertLessEqual(datetime.fromisoformat(start["timestamp"]), datetime.fromisoformat(done["timestamp"]))
        roles = [call.kwargs["role"] for call in self.provider.await_args_list]
        self.assertEqual(roles, ["carbon", "argon", "hydrogen"])

    async def test_failed_analysis_preserves_fallback_decision_before_terminal_state(self):
        result = DecisionResult(gate_name="video_1_relevance", choice="KEEP_TUTORIAL", model_used="fallback-only")
        self.search.return_value = self.videos[:1]
        self.gate.return_value = {result.gate_name: result}
        self.provider.side_effect = RuntimeError("secret provider response")
        with self.assertLogs(level="WARNING"):
            project = await self.run_guide()
        self.assertEqual(project.status, ProjectStatus.FAILED)
        self.assertEqual(project.decisions_log, [result])
        self.assertEqual(self.updates("source-selection")[-1]["model"], "fallback-only")
        self.assertNotIn("Decisions API", self.updates("source-selection")[-1]["agent"])
        self.assertEqual(self.updates("analyze-aaaaaaaaaaa")[-1]["state"], "failed")
        self.assertFalse(self.updates("plan-synthesis"))
        self.assertNotIn("secret provider response", project.model_dump_json())
        names = [name for name, _ in self.events]
        self.assertLess(names.index("research_decisions"), names.index("error"))

    async def test_failed_selection_never_attributes_an_unreturned_decision(self):
        self.gate.side_effect = ValueError("The decision provider declined this input.")
        with self.assertLogs(pipeline.logger, "ERROR"):
            project = await self.run_guide()
        self.assertEqual(project.status, ProjectStatus.FAILED)
        self.assertEqual(project.decisions_log, [])
        start, failed = self.updates("source-selection")
        self.assertEqual((start["state"], failed["state"]), ("started", "failed"))
        self.assertIsNone(failed["model"])
        self.assertNotIn("Decisions API", failed["agent"])
        self.provider.assert_not_awaited()

    async def test_direct_source_omits_discovery_and_invalid_grounding_marks_only_physical_planning_failed(self):
        self.provider.side_effect = [extract(), self.plan, grounding(self.plan, [1])]
        with self.assertLogs(pipeline.logger, "ERROR"):
            project = await self.run_guide(direct_urls=[self.videos[0].url])
        self.search.assert_not_awaited()
        self.gate.assert_not_awaited()
        self.assertEqual(project.status, ProjectStatus.FAILED)
        self.assertEqual([(op.id, op.state) for op in project.activity], [
            ("analyze-aaaaaaaaaaa", "completed"), ("plan-synthesis", "completed"), ("physical-planning", "failed"),
        ])
        self.assertEqual(project.activity[-1].model, "configured-physical-planner")
