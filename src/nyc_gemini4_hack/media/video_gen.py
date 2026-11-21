from __future__ import annotations

from pathlib import Path

from google.genai import types

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import BuildStep, VisualContinuityBible


def source_observations(step: BuildStep) -> str:
    lines = []
    for source in step.source_segments:
        tools = ", ".join(source.tools_visible)
        lines.append(f"{source.segment_id} ({source.start_seconds}–{source.end_seconds}s), {source.technique}: "
                     f"{source.hand_action}. {source.notes or ''} {('Tools: ' + tools) if tools else ''}")
    return "\n".join(lines)


def _image_format(data: bytes) -> tuple[str, str]:
    # Older projects saved every frame as .jpg, including PNG responses.
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise ValueError("Unrecognized POV image format. Expected a PNG, JPEG, or WebP frame.")


async def _generate_pov_frame(
    prompt: str,
    out_path: Path,
    gemini: Gemini4Client,
    reference_image_path: Path | None = None,
) -> Path:
    """Generate one still, preserving the previous frame's physical arrangement."""
    contents = []
    if reference_image_path is not None:
        ref_bytes = reference_image_path.read_bytes()
        mime_type, _ = _image_format(ref_bytes)
        contents.append(types.Part.from_bytes(data=ref_bytes, mime_type=mime_type))
        prompt = (
            "The attached image is the preceding moment. Match its viewpoint, lighting, "
            "hand appearance, object scale, and placement of untouched pieces. "
            "Advance only the change described for this moment.\n" + prompt
        )
    contents.append(prompt)

    model = gemini.settings.pov_image_model
    response = await gemini.client.aio.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"], image_config=types.ImageConfig(aspect_ratio="16:9")
        ),
    )
    for candidate in response.candidates or []:
        if candidate.content is None:
            continue
        for part in candidate.content.parts or []:
            inline = part.inline_data
            if part.thought or inline is None or not inline.data:
                continue
            if inline.mime_type and not inline.mime_type.lower().startswith("image/"):
                continue
            try:
                _, suffix = _image_format(inline.data)
            except ValueError:
                # Other response parts may still contain a usable final frame.
                continue
            frame_path = out_path.with_suffix(suffix)
            frame_path.parent.mkdir(parents=True, exist_ok=True)
            frame_path.write_bytes(inline.data)
            return frame_path

    raise RuntimeError(f"Image model {model} returned no final POV image.")


async def generate_step_pov_frames(
    step: BuildStep,
    bible: VisualContinuityBible,
    out_dir: Path,
    gemini: Gemini4Client,
    prev_step_end_frame: Path | None = None,
    *,
    reviewed_prefix: tuple[bytes, ...] = (),
    correction: str = "",
) -> list[Path]:
    """Render the setup, point of contact, and inspectable result of one step."""
    if len(reviewed_prefix) > 2:
        raise ValueError("Only a reviewed opening and contact frame can seed illustrations.")
    style = (
        "Create one photorealistic landscape still from the maker's point of view. "
        "Frame the workpiece and the area being demonstrated together. Keep the camera fixed. "
        "Exclude captions, diagrams, watermarks and panels.\n"
        f"Camera: {bible.camera_style}. Surface: {bible.workspace_surface}. "
        f"Light: {bible.lighting}. Palette: {bible.subject_palette}. Hands: {bible.hands_description}.\n"
        f"Step {step.step_number}, {step.title}.\n"
    )
    observations = source_observations(step)
    if observations:
        style += f"Preserve the technique documented in these tutorial observations:\n{observations}\n"
    state = step.physical_state
    setup = (
        "Show the instant before the action begins. Preserve completed work from earlier steps.\n"
        "Arrange the hands, supporting surface and loose parts in the specified starting positions.\n"
        f"Starting arrangement: {state.start_state}"
    )
    action = (
        "Show one instant during the technique. For a hands-on action, show the working grip, "
        "support and contact point. For a resting or drying interval, show the piece undisturbed. "
        "Keep the change in progress visible before it reaches the final state.\n"
        f"Starting arrangement: {state.start_state}\n"
        f"Technique: {state.hand_kinematics}\n"
        f"Action detail: {step.pov_video_prompt}"
    )
    result = (
        "Show the work immediately after this action, with its distinguishing features clearly visible. "
        "Keep tools and hands in physically plausible positions.\n"
        f"Result: {state.end_state}"
    )
    if state.verification_cues:
        result += "\nVisible evidence of completion: " + "; ".join(state.verification_cues)
    if correction:
        style += ("A previous take showed the following errors. Follow the planned physical states "
                  "for each illustration and avoid reproducing these errors:\n" + correction + "\n")

    frames = []
    reference = prev_step_end_frame
    out_dir.mkdir(parents=True, exist_ok=True)
    for index, (label, moment) in enumerate((("1_start", setup), ("2_action", action), ("3_end", result))):
        out_path = out_dir / f"step_{step.step_number:02d}_{label}"
        if index < len(reviewed_prefix):
            image = reviewed_prefix[index]
            _, suffix = _image_format(image)
            reference = out_path.with_suffix(suffix)
            reference.write_bytes(image)
        else:
            reference = await _generate_pov_frame(
                prompt=style + moment, out_path=out_path, gemini=gemini, reference_image_path=reference,
            )
        frames.append(reference)
    return frames
