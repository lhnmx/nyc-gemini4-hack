from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from nyc_gemini4_hack.config import get_settings
from nyc_gemini4_hack.models import (
    CheckinResponse,
    CheckinVerdict,
    ProjectEvent,
    ProjectState,
    RebuildCartRequest,
)
from nyc_gemini4_hack.pipeline import run_project_pipeline

WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(
    title="CraftPOV",
    description="Multimodal POV physical-world build guide powered by Gemini 4 & Shopify MCP",
    version="0.1.0",
)

settings = get_settings()
settings.ensure_output_dir()
app.mount("/media", StaticFiles(directory=str(settings.media_output_dir)), name="media")

# In-memory project store & SSE subscriber queues (sufficient for hackathon demo)
_PROJECTS: dict[str, ProjectState] = {}
_SUBSCRIBERS: dict[str, list[asyncio.Queue[ProjectEvent]]] = {}


async def publish_event(project_id: str, event: str, data: dict[str, object]) -> None:
    evt = ProjectEvent(event=event, data=data)
    for queue in list(_SUBSCRIBERS.get(project_id, [])):
        await queue.put(evt)


async def _run_slice1_pipeline(project_id: str) -> None:
    """
    Pipeline entrypoint stub for Slice 1.
    Slices 2–4 plug in:
      - Slice 2: YouTube Research (Carbon) + Planner & Physical State Graph (Argon + Hydrogen)
      - Slice 3: Desk Diff + Shopify MCP Commerce (Neon + Decisions API)
      - Slice 4: Continuity Keyframes + Veo POV Video + Voiceover + FFmpeg Muxing
    """
    project = _PROJECTS[project_id]
    project.status = ProjectStatus.RESEARCHING
    project.status_message = (
        "Slice 1 scaffold active — ready to wire Slice 2 (YouTube + Gemini 4 Carbon/Argon/Hydrogen)"
    )
    await publish_event(
        project_id,
        "status",
        {
            "status": project.status.value,
            "message": project.status_message,
            "readiness": settings.readiness_summary(),
        },
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "app": "CraftPOV",
        **settings.readiness_summary(),
    }


@app.post("/api/projects", status_code=202)
async def create_project(
    goal: str = Form(..., min_length=2, max_length=300),
    owned_items: str = Form(default=""),
    desk_photo: UploadFile | None = File(default=None),
) -> dict[str, object]:
    project_id = uuid.uuid4().hex[:10]
    parsed_owned = [item.strip() for item in owned_items.split(",") if item.strip()]

    desk_photo_url: str | None = None
    if desk_photo is not None and desk_photo.filename:
        project_dir = settings.ensure_output_dir() / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        suffix = Path(desk_photo.filename).suffix or ".jpg"
        dest = project_dir / f"desk_photo{suffix}"
        dest.write_bytes(await desk_photo.read())
        desk_photo_url = f"/media/{project_id}/{dest.name}"

    state = ProjectState(
        project_id=project_id,
        goal=goal.strip(),
        owned_items=parsed_owned,
        desk_photo_url=desk_photo_url,
    )
    _PROJECTS[project_id] = state
    _SUBSCRIBERS[project_id] = []

    asyncio.create_task(run_project_pipeline(state, publish_event=publish_event))

    return {
        "project_id": project_id,
        "status": state.status.value,
        "events_url": f"/api/projects/{project_id}/events",
        "desk_photo_url": desk_photo_url,
    }


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> ProjectState:
    project = _PROJECTS.get(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@app.get("/api/projects/{project_id}/events")
async def stream_project_events(project_id: str) -> StreamingResponse:
    if project_id not in _PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")

    queue: asyncio.Queue[ProjectEvent] = asyncio.Queue()
    _SUBSCRIBERS.setdefault(project_id, []).append(queue)

    async def event_generator() -> AsyncGenerator[str, None]:
        try:
            current = _PROJECTS[project_id]
            init_payload = json.dumps(
                {
                    "status": current.status.value,
                    "message": current.status_message,
                }
            )
            yield f"event: status\ndata: {init_payload}\n\n"
            while True:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"event: {evt.event}\ndata: {json.dumps(evt.data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            if queue in _SUBSCRIBERS.get(project_id, []):
                _SUBSCRIBERS[project_id].remove(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/projects/{project_id}/cart/rebuild")
async def rebuild_cart(project_id: str, body: RebuildCartRequest) -> dict[str, object]:
    if project_id not in _PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")
    return {
        "project_id": project_id,
        "owned_material_ids": body.owned_material_ids,
        "message": "Shopify MCP cart rebuild will be wired in Slice 3.",
    }


@app.post("/api/projects/{project_id}/steps/{step_number}/checkin")
async def step_checkin(
    project_id: str,
    step_number: int,
    frame: UploadFile = File(...),
) -> CheckinResponse:
    if project_id not in _PROJECTS:
        raise HTTPException(status_code=404, detail="Project not found")
    _ = await frame.read()
    return CheckinResponse(
        step_number=step_number,
        verdict=CheckinVerdict.ADJUST,
        feedback_text="Live Hydrogen + Neon workbench check-in will be wired in Slice 4.",
    )
