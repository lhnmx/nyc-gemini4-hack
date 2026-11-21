from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI) -> None:
    source = Path(__file__).resolve()
    root = source.parents[2]
    dist = root / "frontend" / "dist"
    app.mount("/static/example", StaticFiles(directory=source.parent / "web" / "example"), name="example")
    app.mount("/assets", StaticFiles(directory=dist / "assets", check_dir=False), name="assets")

    @app.api_route("/ucp-agent-profile.json", methods=["GET", "HEAD"], include_in_schema=False)
    async def agent_profile():
        return FileResponse(root / "ucp-agent-profile.json", media_type="application/json")

    @app.get("/", include_in_schema=False)
    async def index():
        if not (dist / "index.html").is_file():
            return PlainTextResponse("Build the workbench from the repository root with npm --prefix frontend ci "
                                     "and npm --prefix frontend run build.", status_code=503)
        return FileResponse(dist / "index.html")
