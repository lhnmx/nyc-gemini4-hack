import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from nyc_gemini4_hack.job_store import JobStoreError, _turso_batch, open_job_store
from nyc_gemini4_hack.models import (
    MaterialItem, MaterialStatus, ProjectState, ProjectStatus, ShoppingCartProposal, StepMediaAsset,
)
from tests.test_pipeline import paper_plan


class JobStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Path(self.enterContext(tempfile.TemporaryDirectory())) / "nested" / "guides.sqlite"
        self.writer = await self.enterAsyncContext(open_job_store(self.db))
        self.reader = await self.enterAsyncContext(open_job_store(self.db))

    async def test_independent_workers_share_state_and_never_double_claim(self):
        states = [ProjectState(project_id=f"fold-{number}", goal="Fold a glider") for number in range(2)]
        await self.writer.enqueue(states[0])
        await self.reader.enqueue(states[1])
        workers = ["worker-A", "worker-B"]
        claimed = await asyncio.gather(self.writer.claim(workers[0]), self.reader.claim(workers[1]))
        self.assertEqual({state.project_id for state in claimed}, {"fold-0", "fold-1"})
        self.assertTrue(await self.reader.online())
        self.assertIsNone(await self.writer.claim("worker-C"))
        self.assertIsNone(await self.reader.read("missing"))
        state = claimed[0]
        self.assertFalse(await self.reader.heartbeat(workers[1], state.project_id))
        self.assertTrue(await self.reader.heartbeat(workers[0], state.project_id))
        state.status = ProjectStatus.GENERATING
        state.plan = paper_plan()
        self.assertFalse(await self.reader.checkpoint(workers[1], state, "plan_ready", {}))
        self.assertTrue(await self.reader.checkpoint(workers[0], state, "plan_ready", state.plan.model_dump()))
        self.assertEqual(await self.writer.read(state.project_id), (state, 1))
        state.status = ProjectStatus.COMPLETED
        self.assertTrue(await self.writer.checkpoint(workers[0], state, "status", {"status": "completed"}))
        self.assertFalse(await self.writer.checkpoint(workers[0], state, "status", {"status": "completed"}))

        async with open_job_store(self.db) as restarted:
            self.assertEqual(await restarted.read(state.project_id), (state, 2))
            self.assertEqual(await restarted.events_after(state.project_id, 1),
                             [{"revision": 2, "event": "status", "data": {"status": "completed"}}])
            self.assertEqual([event["event"] for event in await restarted.events_after(state.project_id, 0)],
                             ["plan_ready", "status"])

    async def test_expired_owners_leave_one_terminal_event_and_preserve_media(self):
        first = ProjectState(project_id="fold-first", goal="Fold a glider")
        second = ProjectState(project_id="fold-second", goal="Fold a glider")
        await self.writer.enqueue(first)
        await self.writer.enqueue(second)
        self.assertEqual(await self.writer.claim("A"), first)
        self.assertEqual(await self.writer.claim("B"), second)
        first.status = ProjectStatus.GENERATING
        first.plan = paper_plan()
        first.media[1] = StepMediaAsset(step_number=1, status="ready", video_url="/media/fold-first/step1.mp4")
        await self.writer.checkpoint("A", first, "step_media_ready", first.media[1].model_dump())
        with closing(sqlite3.connect(self.db)) as db:
            with db:
                db.execute("UPDATE craftpov_projects SET lease_until=0")
                db.execute("UPDATE craftpov_presence SET seen=0")
        self.assertFalse(await self.reader.online())
        self.assertFalse(await self.reader.checkpoint("A", first, "status", {}))
        self.assertFalse(await self.reader.heartbeat("A", first.project_id))
        self.assertIsNone(await self.writer.claim("C"))
        self.assertIsNone(await self.writer.claim("D"))
        recovered, revision = await self.reader.read(first.project_id)
        self.assertEqual((recovered.status, revision, recovered.media, recovered.plan),
                         (ProjectStatus.PARTIAL, 2, first.media, first.plan))
        recovered, revision = await self.reader.read(second.project_id)
        self.assertEqual((recovered.status, revision), (ProjectStatus.FAILED, 1))
        updates = await self.reader.events_after(first.project_id, 1)
        self.assertEqual([(e["event"], e["data"]["status"]) for e in updates], [("status", "partial")])

    async def test_user_cart_and_chapter_progress_survive_interleaved_writes(self):
        await self.writer.enqueue(ProjectState(project_id="paper", goal="Fold a glider"))
        render = await self.writer.claim("worker")
        render.plan = paper_plan()
        render.plan.materials = [MaterialItem(id="paper", name="Paper", search_query="origami paper")]
        render.status = ProjectStatus.GENERATING
        await self.writer.checkpoint("worker", render, "plan_ready", render.plan.model_dump())

        # The browser starts rebuilding the cart, then a new chapter is published.
        editor, _ = await self.reader.read(render.project_id)
        render.media[1] = StepMediaAsset(step_number=1, status="ready", video_url="/media/paper/first.mp4")
        await self.writer.checkpoint("worker", render, "step_media_ready", render.media[1].model_dump())
        editor.plan.materials[0].status = MaterialStatus.OWNED_EXACT
        editor.cart = ShoppingCartProposal(cart_id="user-selection")
        self.assertTrue(await self.reader.update_cart(editor))
        saved, revision = await self.reader.read(render.project_id)
        self.assertEqual(saved.media, render.media)

        # A late automatic proposal and the worker's old materials must not replace the user's cart.
        render.cart = ShoppingCartProposal(cart_id="automatic")
        self.assertTrue(await self.writer.checkpoint("worker", render, "cart_ready", render.cart.model_dump()))
        self.assertEqual(await self.reader.read(render.project_id), (saved, revision))
        render.status = ProjectStatus.COMPLETED
        self.assertTrue(await self.writer.checkpoint("worker", render, "status", {"status": "completed"}))
        saved, _ = await self.reader.read(render.project_id)
        self.assertEqual((saved.status, saved.cart, saved.plan.materials), (render.status, editor.cart, editor.plan.materials))
        events = await self.reader.events_after(render.project_id, 0)
        self.assertEqual([e["event"] for e in events], ["plan_ready", "step_media_ready", "cart_ready", "status"])
        self.assertEqual(events[2]["data"], editor.cart.model_dump(mode="json"))

    async def test_concurrent_first_use_and_duplicate_intake(self):
        state = ProjectState(project_id="once", goal="Make a guide")
        results = await asyncio.gather(self.writer.enqueue(state), self.reader.enqueue(state), return_exceptions=True)
        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, JobStoreError) for result in results), 1)
        self.assertEqual(await self.writer.read(state.project_id), (state, 0))

    async def test_remote_batches_gate_commit_and_report_database_errors(self):
        def respond(request):
            self.assertEqual(str(request.url), "https://example.turso.io/v3/pipeline")
            batch, close = json.loads(request.content)["requests"]
            self.assertEqual(close, {"type": "close"})
            steps = batch["batch"]["steps"]
            self.assertEqual([s["stmt"]["sql"] for s in steps], ["BEGIN IMMEDIATE", "INSERT", "SELECT", "COMMIT"])
            self.assertEqual(steps[1]["stmt"]["args"], [{"type": "text", "value": "guide"}])
            self.assertEqual(steps[2]["stmt"]["args"], [{"type": "integer", "value": "1"}])
            self.assertEqual([step["condition"] for step in steps[1:]], [{"type": "ok", "step": i} for i in range(3)])
            return httpx.Response(200, json={"results": [
                {"type": "ok", "response": {"type": "batch", "result": {
                    "step_errors": [None, {"message": "constraint failed"}, None, None],
                    "step_results": [{"rows": []}, None, None, None],
                }}}, {"type": "ok", "response": {"type": "close"}}]})
        async with httpx.AsyncClient(base_url="https://example.turso.io/", transport=httpx.MockTransport(respond)) as client:
            with self.assertRaisesRegex(JobStoreError, "constraint failed"):
                await _turso_batch(client, [("INSERT", ("guide",)), ("SELECT", (1,))])
        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            with self.assertRaisesRegex(JobStoreError, "TURSO_DATABASE_URL"):
                async with open_job_store(None):
                    self.fail("Expected missing configuration error")
