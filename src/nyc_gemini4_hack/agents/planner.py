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
    owned_str = json.dumps(owned_items, ensure_ascii=False)

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
        "clean Shopify-friendly search_query strings (e.g. 'double sided origami paper'), "
        "and a material name that matches the user's wording when it names the same item.\n"
        "- steps: order the dependencies and number the steps consecutively from 1. For each step:\n"
        "  * narration_script: what the maker should hear while watching. Start with the action verb, "
        "name its target, then give one concrete cue for when to stop. Aim for 10–14 spoken words "
        "in a six-second clip. Use everyday speech, including units spoken in full. Omit greetings, "
        "step numbers, camera directions and performance notes. Example: "
        "'Fold the bottom corner up until the two edges meet.'\n"
        "  * instruction_text: give the full instruction, including real waiting times, measurements "
        "and any necessary precautions; keep these intact even when the narration is shorter.\n"
        "  * pov_video_prompt: describe one visible action from the maker's viewpoint.\n"
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

    # Source links and inventory come from the actual inputs, independently of model guesses.
    source_urls = list(dict.fromkeys(extract.url for extract in extracts))
    cited = dict.fromkeys(plan.source_youtube_urls)
    plan.source_youtube_urls = [url for url in cited if url in source_urls] or source_urls
    owned_names = {" ".join(item.casefold().split()) for item in owned_items if item.strip()}
    for mat in plan.materials:
        name = " ".join(mat.name.casefold().split())
        mat.status = MaterialStatus.OWNED_EXACT if name in owned_names else MaterialStatus.BUY
        mat.substitute_note = None

    # Stage 2: Gemini 4 Hydrogen — World-model physical state & POV continuity grounding
    hydrogen_system = (
        "You direct a first-person craft tutorial filmed from one fixed position. "
        "Plan each chapter of one continuous tutorial film. Show the initial setup, hand or tool contact in motion, and the resulting state. "
        "Keep material, part count, scale, orientation, and completed work consistent across frames. "
        "Keep elapsed real-world processes such as drying or cooling explicit in the state descriptions."
    )
    hydrogen_prompt = (
        f"Refine the VisualContinuityBible and every step's PhysicalStateTransition + pov_video_prompt "
        f"for this BuildPlan:\n\n{plan.model_dump_json(indent=2)}\n\n"
        "Rules:\n"
        "1. Put the stable surface, lighting, eye-line, palette and hand appearance in continuity_bible. "
        "Choose a view that keeps the working area visible for this craft.\n"
        "2. Return one update per existing step_number in plan order. Carry the previous end_state "
        "into the next start_state, accounting for any required waiting period.\n"
        "3. In hand_kinematics, identify what steadies the work, where the active hand or tool "
        "makes contact, and its direction and pressure when relevant. For drying or cooling, leave the work undisturbed and make the elapsed time explicit.\n"
        "4. Give 2–3 visible verification_cues for the result, such as edge alignment or outline.\n"
        "5. Write pov_video_prompt as a short visual description of the single observable action. "
        "Keep the same camera position and show only this step’s tools and parts."
    )

    grounded = await gemini.generate_structured(
        role="hydrogen",
        contents=hydrogen_prompt,
        response_schema=_HydrogenGroundingOutput,
        system_instruction=hydrogen_system,
        temperature=0.15,
    )

    updates_by_num = {s.step_number: s for s in grounded.steps}
    expected = {step.step_number for step in plan.steps}
    if len(grounded.steps) != len(expected) or updates_by_num.keys() != expected:
        raise ValueError("Grounding must provide exactly one physical transition per planned step.")

    plan.continuity_bible = grounded.continuity_bible
    for step in plan.steps:
        update = updates_by_num[step.step_number]
        step.pov_video_prompt = update.pov_video_prompt
        step.physical_state = update.physical_state

    return plan
