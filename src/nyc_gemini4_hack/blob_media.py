from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path

from vercel import blob

from nyc_gemini4_hack.models import ProjectState


def configured() -> bool:
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN") or
                (os.getenv("BLOB_STORE_ID") and os.getenv("VERCEL_OIDC_TOKEN")))


def media_names(state: ProjectState) -> set[str]:
    """Only expose finished files explicitly published in this project's snapshot."""
    prefix = f"/media/{state.project_id}/"
    records = [state.model_dump(exclude={"media"}), *(asset.model_dump() for asset in state.media.values())]
    names = set()
    for record in records:
        for key, value in record.items():
            if key.endswith("_url") and isinstance(value, str) and value.startswith(prefix):
                name = value.removeprefix(prefix)
                if name and name not in {".", ".."} and Path(name).name == name:
                    names.add(name)
    return names


async def upload(project_id: str, name: str, content: bytes | Path) -> None:
    async def chunks(path: Path):
        with path.open("rb") as file:
            while block := await asyncio.to_thread(file.read, 256 * 1024):
                yield block

    body = chunks(content) if isinstance(content, Path) else content
    size = content.stat().st_size if isinstance(content, Path) else len(content)
    await blob.put_blob(f"guides/{project_id}/{name}", body, access="public", content_length=size,
                        content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                        add_random_suffix=False, allow_overwrite=True)


async def delivery_url(project_id: str, filename: str) -> str:
    metadata = await blob.head_blob(f"guides/{project_id}/{filename}")
    return metadata.url
