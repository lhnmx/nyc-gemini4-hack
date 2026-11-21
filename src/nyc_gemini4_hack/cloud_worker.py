"""Claim SQLite jobs and publish their snapshots after their media is available."""
from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from nyc_gemini4_hack import blob_media
from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.job_store import JobStore, open_job_store
from nyc_gemini4_hack.models import ProjectState, ProjectStatus
from nyc_gemini4_hack.pipeline import run_project_pipeline

logger = logging.getLogger(__name__)


async def render(store: JobStore, worker_id: str, project: ProjectState, settings: Settings, *, hosted: bool) -> None:
    # Intake has already uploaded the desk photo. The pipeline publishes the rest.
    published = blob_media.media_names(project)
    finished = False

    async def checkpoint(job_id: str, event: str, data: dict) -> None:
        nonlocal finished
        if hosted:
            for filename in sorted(blob_media.media_names(project) - published):
                await blob_media.upload(job_id, filename, settings.media_output_dir / job_id / filename)
                published.add(filename)
        if not await store.checkpoint(worker_id, project, event, data):
            raise RuntimeError("The worker no longer owns this job.")
        finished = event == "status" and project.status.terminal

    job = asyncio.create_task(run_project_pipeline(project, publish_event=checkpoint))
    try:
        while not job.done():
            done, _ = await asyncio.wait([job], timeout=20)
            if not done and not finished and not await store.heartbeat(worker_id, project.project_id):
                raise RuntimeError("The job lease expired.")
        await job
        if not finished:
            raise RuntimeError("Generation stopped before a terminal checkpoint.")
    except (Exception, asyncio.CancelledError) as exc:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        # An upload or provider can fail partway through. Keep the last playable snapshot.
        row = await store.read(project.project_id)
        if row and not row[0].status.terminal:
            state, _ = row
            state.status = ProjectStatus.PARTIAL if state.plan else ProjectStatus.FAILED
            state.error = state.status_message = "Guide generation was interrupted. Create a new guide to retry."
            await store.checkpoint(worker_id, state, "status", {
                "status": state.status.value, "message": state.status_message,
            })
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.exception("Could not finish guide %s", project.project_id)


async def consume(store: JobStore, settings: Settings, wake: asyncio.Event, stopping: asyncio.Event,
                  *, hosted: bool) -> None:
    worker_id = str(uuid4())
    while not stopping.is_set():
        try:
            wake.clear()
            project = await store.claim(worker_id)
            if project:
                if hosted:
                    # Only the standalone daemon uses a temporary workspace and one job at a time.
                    with TemporaryDirectory(prefix="craftpov-render-") as workspace:
                        settings.media_output_dir = Path(workspace)
                        await render(store, worker_id, project, settings, hosted=True)
                else:
                    await render(store, worker_id, project, settings, hosted=False)
                continue
        except Exception:
            logger.exception("Could not retrieve or checkpoint a guide")
        try:
            await asyncio.wait_for(wake.wait(), timeout=8)
        except TimeoutError:
            pass


async def serve() -> None:
    settings = get_settings()
    if not blob_media.configured() or not settings.gemini_api_key.strip() or not settings.youtube_api_key.strip():
        raise SystemExit("The rendering worker requires Blob, Gemini and YouTube configuration.")
    wake, stopping = asyncio.Event(), asyncio.Event()

    def shutdown():
        stopping.set()
        wake.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    async with open_job_store(None) as store:
        await consume(store, settings, wake, stopping, hosted=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
