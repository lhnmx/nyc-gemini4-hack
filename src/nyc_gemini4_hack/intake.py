from uuid import uuid4

from fastapi import HTTPException, UploadFile

from nyc_gemini4_hack.models import ProjectState


async def prepare_project(
    goal: str, owned_items: str, desk_photo: UploadFile | None, *, photo_limit_mib: int
) -> tuple[ProjectState, bytes | None]:
    goal = goal.strip()
    if len(goal) < 2:
        raise HTTPException(422, "Describe your project in at least two characters.")
    project = ProjectState(project_id=uuid4().hex, goal=goal,
                           owned_items=[item.strip() for item in owned_items.split(",") if item.strip()])
    if not desk_photo or not desk_photo.filename:
        return project, None
    limit = photo_limit_mib * 1024 * 1024
    data = await desk_photo.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"Desk photos must be {photo_limit_mib} MB or smaller.")
    if data.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise HTTPException(415, "Choose a JPEG, PNG or WebP desk photo.")
    project.desk_photo_url = f"/media/{project.project_id}/desk_photo{suffix}"
    return project, data


def creation_response(project: ProjectState) -> dict[str, object]:
    return {"project_id": project.project_id, "status": project.status.value,
            "events_url": f"/api/projects/{project.project_id}/events",
            "desk_photo_url": project.desk_photo_url}
