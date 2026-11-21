from __future__ import annotations

import uvicorn

from nyc_gemini4_hack.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "nyc_gemini4_hack.api:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
