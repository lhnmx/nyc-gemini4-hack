from __future__ import annotations

import json

from pydantic import BaseModel, Field

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import (
    BuildPlan,
    MaterialStatus,
    PhysicalStateTransition,
    TutorialExtract,
    VisualContinuityBible,
)


class _GroundedStepUpdate(BaseModel):
    step_number: int
    pov_video_prompt: str
    physical_state: PhysicalStateTransition


class _HydrogenGroundingOutput(BaseModel):
    continuity_bible: VisualContinuityBible
    steps: list[_GroundedStepUpdate] = Field(default_factory=list)


async def synthesize_build_plan(
    goal: str,
    extracts: list[TutorialExtract],
    owned_items: list[str],
    gemini: Gemini4Client,
) -> BuildPlan:
    """
    Two-stage physical planning pipeline:
      1. Gemini 4 Argon (Deep Reasoning): Synthesizes multi-video extracts into one
         physically valid BuildPlan, deduplicated BOM, and concise voice narration scripts.
      2. Gemini 4 Hydrogen (World-Model Grounding): Locks the VisualContinuityBible and
         enforces coherent 3D PhysicalStateTransitions (start_state -> hand_kinematics -> end_state)
         and first-person POV video prompts across every step.
    """
    extracts_json = json.dumps([e.model_dump() for e in extracts], indent=2)
    owned_str = ", ".join(owned_items) if owned_items else "none specified"

    # Stage 1: Gemini 4 Argon — Deep physical synthesis & BOM
    argon_system = (
        "You are a master craftsperson and physical fabrication planner (Gemini 4 Argon). "
        "Synthesize observations from multiple real YouTube tutorials into ONE clear, beginner-friendly, "
        "physically accurate BuildPlan. Respect real-world physics (structural support, joint attachment, "
        "proportions, curing/drying order). Keep the build to 5–7 atomic, actionable steps."
    )
    argon_prompt = (
        f"User Goal: {goal}\n"
        f"Items user says they already own: {owned_str}\n\n"
        f"Observed YouTube Tutorial Extracts:\n{extracts_json}\n\n"
        "Generate a complete BuildPlan JSON:\n"
        "- source_youtube_urls: list of the source URLs used.\n"
        "- materials: deduplicated Bill of Materials with short unique ids (e.g. 'mat_1'), "
        "clean Shopify-friendly search_query strings (e.g. 'green polymer clay'), "
        "and status='OWNED_EXACT' if clearly covered by the user's owned list, else 'BUY'.\n"
        "- steps: 5 to 7 atomic steps. For each step:\n"
        "  * narration_script: crisp, warm spoken instruction (~12–18 words, fits a 6-second POV clip).\n"
        "  * instruction_text: 1–2 sentences of clear written guidance.\n"
        "  * pov_video_prompt: first-person over-the-hands POV description.\n"
        "  * physical_state: start_state, hand_kinematics, end_state, verification_cues.\n"
        "  * duration_seconds: 6."
    )

    plan = await gemini.generate_structured(
        role="argon",
        contents=argon_prompt,
        response_schema=BuildPlan,
        system_instruction=argon_system,
        temperature=0.25,
    )

    # Ensure source URLs are preserved
    if not plan.source_youtube_urls:
        plan.source_youtube_urls = [e.url for e in extracts]

    # Normalize owned status for explicit text matches
    owned_lower = [o.lower() for o in owned_items]
    for mat in plan.materials:
        if any(o in mat.name.lower() or mat.name.lower() in o for o in owned_lower):
            mat.status = MaterialStatus.OWNED_EXACT

    # Stage 2: Gemini 4 Hydrogen — World-model physical state & POV continuity grounding
    hydrogen_system = (
        "You are a physical world-model and spatial continuity specialist (Gemini 4 Hydrogen). "
        "Your job is to ensure that a sequence of first-person (POV) video generation prompts "
        "looks like ONE continuous real-world shoot with zero visual or physical drift: "
        "Step N's start_state MUST physically match Step N-1's end_state, hand kinematics must be "
        "anatomically and physically plausible, and every pov_video_prompt must embed the exact "
        "workspace surface, lighting, camera tilt, and subject palette."
    )
    hydrogen_prompt = (
        f"Refine the VisualContinuityBible and every step's PhysicalStateTransition + pov_video_prompt "
        f"for this BuildPlan:\n\n{plan.model_dump_json(indent=2)}\n\n"
        "Rules:\n"
        "1. Lock workspace_surface, lighting, camera_style ('First-person POV looking down at 45 degrees...'), "
        "subject_palette, and hands_description.\n"
        "2. For every step_number, verify Step i start_state seamlessly continues Step i-1 end_state.\n"
        "3. Write vivid, concrete hand_kinematics (finger grip, motion path, pressure).\n"
        "4. Include 2–3 concrete visual verification_cues per step that a webcam check-in can inspect.\n"
        "5. Write each pov_video_prompt as a self-contained first-person POV video prompt."
    )

    grounded = await gemini.generate_structured(
        role="hydrogen",
        contents=hydrogen_prompt,
        response_schema=_HydrogenGroundingOutput,
        system_instruction=hydrogen_system,
        temperature=0.15,
    )

    plan.continuity_bible = grounded.continuity_bible
    updates_by_num = {s.step_number: s for s in grounded.steps}
    for step in plan.steps:
        upd = updates_by_num.get(step.step_number)
        if upd is not None:
            step.pov_video_prompt = upd.pov_video_prompt
            step.physical_state = upd.physical_state

    return plan
