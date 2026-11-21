from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from google.genai import types

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import BuildStep, VisualContinuityBible

logger = logging.getLogger(__name__)


async def _generate_pov_frame(
    prompt: str,
    out_path: Path,
    gemini: Gemini4Client,
    reference_image_path: Path | None = None,
) -> Path:
    """
    Generate a single photorealistic first-person POV frame using Gemini image generation
    (gemini-3.1-flash-image), optionally conditioned on the previous frame for strict
    workbench, lighting, hands, and clay geometry continuity.
    """
    model = gemini.settings.pov_image_model
    contents: list[object] = []

    if reference_image_path is not None and reference_image_path.exists():
        ref_bytes = reference_image_path.read_bytes()
        contents.append(
            types.Part.from_bytes(data=ref_bytes, mime_type="image/jpeg")
        )
        contents.append(
            "CONTINUITY REFERENCE IMAGE ABOVE: Keep the exact same cutting mat, camera angle "
            "(first-person POV looking down at 45 degrees), lighting, hand appearance, and clay color/scale "
            "from the reference image, progressing naturally to the next moment described below.\n\n"
            + prompt
        )
    else:
        contents.append(prompt)

    def _call() -> object:
        return gemini.client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

    response = await asyncio.to_thread(_call)
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        parts = getattr(cand.content, "parts", None) or []
        for part in parts:
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(inline.data)
                return out_path

    raise RuntimeError(f"Model {model} returned no image bytes for prompt: {prompt[:120]}")


async def generate_step_pov_frames(
    step: BuildStep,
    bible: VisualContinuityBible,
    out_dir: Path,
    gemini: Gemini4Client,
    prev_step_end_frame: Path | None = None,
) -> list[Path]:
    """
    Generate a 3-frame continuity-chained POV motion sequence (Start -> Action -> End)
    for a single build step so the resulting video shows smooth physical progression.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    style_header = (
        f"Photorealistic 16:9 first-person POV craft photography. "
        f"Camera: {bible.camera_style}. "
        f"Surface: {bible.workspace_surface}. "
        f"Lighting: {bible.lighting}. "
        f"Palette: {bible.subject_palette}. "
        f"Hands: {bible.hands_description}. "
        "No text, no watermarks, no split screen."
    )

    start_path = out_dir / f"step_{step.step_number:02d}_1_start.jpg"
    mid_path = out_dir / f"step_{step.step_number:02d}_2_action.jpg"
    end_path = out_dir / f"step_{step.step_number:02d}_3_end.jpg"

    # Frame 1: Start state (chained from previous step's End frame if available)
    start_prompt = (
        f"{style_header}\n"
        f"MOMENT: Beginning of Step {step.step_number} ({step.title}).\n"
        f"SCENE STATE: {step.physical_state.start_state}"
    )
    await _generate_pov_frame(
        prompt=start_prompt,
        out_path=start_path,
        gemini=gemini,
        reference_image_path=prev_step_end_frame,
    )

    # Frame 2: Mid-action hands performing the physical technique
    mid_prompt = (
        f"{style_header}\n"
        f"MOMENT: Mid-action during Step {step.step_number} ({step.title}).\n"
        f"HAND KINEMATICS: {step.physical_state.hand_kinematics}\n"
        f"ACTION DETAIL: {step.pov_video_prompt}"
    )
    await _generate_pov_frame(
        prompt=mid_prompt,
        out_path=mid_path,
        gemini=gemini,
        reference_image_path=start_path,
    )

    # Frame 3: Completed End state of this step
    end_prompt = (
        f"{style_header}\n"
        f"MOMENT: Completed result of Step {step.step_number} ({step.title}).\n"
        f"END STATE: {step.physical_state.end_state}"
    )
    await _generate_pov_frame(
        prompt=end_prompt,
        out_path=end_path,
        gemini=gemini,
        reference_image_path=mid_path,
    )

    return [start_path, mid_path, end_path]
