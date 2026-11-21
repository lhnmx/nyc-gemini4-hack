"""The public API is identical for a local SQLite file and hosted SQLite."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from vercel import blob

from nyc_gemini4_hack import blob_media
from nyc_gemini4_hack.agents.commerce import (
    build_shopping_cart_proposal,
    create_mock_store_cart,
    search_shopify_products,
)
from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.intake import creation_response, prepare_project
from nyc_gemini4_hack.job_store import JobStore, JobStoreError, open_job_store
from nyc_gemini4_hack.models import (
    MaterialStatus,
    ProjectState,
    ProjectStatus,
    RebuildCartRequest,
    ShopifyCartRequest,
    ShopifySearchRequest,
    ShoppingCartProposal,
)

ProjectId = Annotated[str, ApiPath(pattern=r"^[a-zA-Z0-9_-]+$", max_length=96)]


def store_connection(request: Request) -> JobStore:
    store = request.app.state.store
    if store is None:
        raise HTTPException(503, "Guide storage needs configuration.")
    return store


Store = Annotated[JobStore, Depends(store_connection)]


async def load_project(project_id: ProjectId, store: Store) -> tuple[ProjectState, int]:
    row = await store.read(project_id)
    if not row:
        raise HTTPException(404, "Project not found")
    return row


Loaded = Annotated[tuple[ProjectState, int], Depends(load_project)]


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def create_app(*, hosted: bool = False, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            app.state.store = None
            if not hosted or (os.getenv("TURSO_DATABASE_URL") and os.getenv("TURSO_AUTH_TOKEN")):
                app.state.store = await stack.enter_async_context(open_job_store(None if hosted else settings.sqlite_path))
            job = None
            if not hosted and settings.gemini_api_key.strip() and settings.youtube_api_key.strip():
                from nyc_gemini4_hack.cloud_worker import consume
                app.state.wake = asyncio.Event()
                job = asyncio.create_task(consume(app.state.store, settings, app.state.wake, asyncio.Event(), hosted=False))
            try:
                yield
            finally:
                if job is not None:
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)

    app = FastAPI(title="CraftPOV", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(httpx.HTTPError)
    @app.exception_handler(JobStoreError)
    @app.exception_handler(blob.BlobError)
    async def storage_unavailable(request, exc):
        return JSONResponse({"detail": "Guide storage is unavailable. Try again shortly."}, status_code=503)

    async def readiness(request: Request) -> dict:
        provider = settings.readiness_summary()
        store = request.app.state.store
        message = ""
        if store is None:
            message = "Guide storage needs configuration."
        elif hosted and not blob_media.configured():
            message = "Media storage needs configuration."
        elif not (provider["gemini_configured"] and provider["youtube_configured"]):
            message = "Guide generation needs the Gemini and YouTube API keys on the server."
        elif hosted and not await store.online():
            message = "The guide generation worker is offline."
        return {"ok": True, "app": "CraftPOV", **provider, "generation_ready": not message,
                "generation_message": message or "Guide generation services are ready.",
                "max_desk_photo_bytes": (4 if hosted else 8) * 1024 * 1024}

    app.get("/api/health")(readiness)

    @app.post("/api/projects", status_code=202)
    async def create_project(request: Request, store: Store, goal: str = Form(..., min_length=2, max_length=300),
                             owned_items: str = Form(default=""), desk_photo: UploadFile | None = File(default=None)):
        state, photo = await prepare_project(goal, owned_items, desk_photo, photo_limit_mib=4 if hosted else 8)
        health = await readiness(request)
        if not health["generation_ready"]:
            raise HTTPException(503, health["generation_message"])
        if photo is not None:
            filename = state.desk_photo_url.rsplit("/", 1)[1]
            if hosted:
                await blob_media.upload(state.project_id, filename, photo)
            else:
                folder = settings.ensure_output_dir() / state.project_id
                folder.mkdir(exist_ok=True)
                await asyncio.to_thread((folder / filename).write_bytes, photo)
        await store.enqueue(state)
        if not hosted:
            request.app.state.wake.set()
        return creation_response(state)

    @app.get("/api/projects/{project_id}")
    async def get_project(row: Loaded) -> ProjectState:
        return row[0]

    @app.get("/api/projects/{project_id}/events")
    async def stream_project_events(row: Loaded, store: Store):
        state, last_revision = row

        async def events():
            revision = last_revision
            yield sse("snapshot", state.model_dump(mode="json"))
            yield sse("status", {"status": state.status.value, "message": state.status_message})
            if state.status.terminal:
                return
            # EventSource reconnects before the hosted function's time limit and recovers a complete snapshot.
            poll = 0
            while not hosted or poll < 20:
                poll += 1
                await asyncio.sleep(2 if hosted else 0.5)
                for update in await store.events_after(state.project_id, revision):
                    revision = update["revision"]
                    name, data = update["event"], update["data"]
                    yield sse("project_error" if name == "error" else name, data)
                    if name == "status" and ProjectStatus(data["status"]).terminal:
                        return
                if poll % (5 if hosted else 30) == 0:
                    yield ": keep-alive\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.api_route("/media/{project_id}/{filename}", methods=["GET", "HEAD"], include_in_schema=False)
    async def media(project_id: ProjectId, store: Store, filename: str):
        if not hosted:
            # Earlier API runs and CLI renders have files on disk without a SQLite row.
            root = settings.media_output_dir.resolve()
            path = (root / project_id / filename).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise HTTPException(404, "Media not found")
            return FileResponse(path)
        state, _ = await load_project(project_id, store)
        if filename not in blob_media.media_names(state):
            raise HTTPException(404, "Media not found")
        try:
            url = await blob_media.delivery_url(project_id, filename)
        except blob.BlobNotFoundError:
            raise HTTPException(404, "Media not found") from None
        return RedirectResponse(url, headers={"Cache-Control": "private, no-store"})

    @app.post("/api/projects/{project_id}/cart/rebuild", response_model=ShoppingCartProposal)
    async def rebuild_cart(row: Loaded, store: Store, body: RebuildCartRequest) -> ShoppingCartProposal:
        project, _ = row
        if project.plan is None:
            raise HTTPException(status_code=400, detail="Project plan is not ready yet")

        owned_set = set(body.owned_material_ids)
        for mat in project.plan.materials:
            mat.status = MaterialStatus.OWNED_EXACT if mat.id in owned_set else MaterialStatus.BUY

        cart = await build_shopping_cart_proposal(
            materials=project.plan.materials,
            owned_material_ids=owned_set,
        )
        project.cart = cart
        await store.update(project, "cart_ready", cart.model_dump(mode="json"))
        return cart

    @app.post("/api/shopify/search")
    async def shopify_search(body: ShopifySearchRequest) -> dict[str, object]:
        """Reusable standalone endpoint: search Global Catalog MCP and/or mock dev store."""
        results = await search_shopify_products(
            query=body.query,
            limit=body.limit,
            scope=body.scope,
        )
        return {
            "query": body.query,
            "scope": body.scope,
            "global_options": [opt.model_dump() for opt in results["global"]],
            "store_options": [opt.model_dump() for opt in results["store"]],
        }

    @app.post("/api/shopify/cart", response_model=ShoppingCartProposal)
    async def shopify_create_cart(body: ShopifyCartRequest) -> ShoppingCartProposal:
        """Reusable standalone endpoint: create a live checkout cart on the mock dev store."""
        try:
            return await create_mock_store_cart(body.items)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/projects/{project_id}/steps/{step_number}/checkin")
    async def step_checkin(row: Loaded, step_number: int, frame: UploadFile = File(...)):
        raise HTTPException(501, "Camera-based progress checks are unavailable.")

    return app
