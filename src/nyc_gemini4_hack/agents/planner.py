from __future__ import annotations

import json

from pydantic import BaseModel, Field

from nyc_gemini4_hack.agents.researcher import ActivityReporter, agent_operation
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import (
    BuildPlan, BuildStep, MaterialItem, MaterialStatus, PhysicalStateTransition,
    TutorialExtract, VisualContinuityBible,
)


class _WrittenStep(BaseModel):
    step_number: int = Field(ge=1)
    title: str
    instruction_text: str
    narration_script: str
    tips: list[str] = Field(default_factory=list)
    duration_seconds: int = Field(default=6, ge=1)


class _WrittenPlan(BaseModel):
    """The writer only generates fields that survive physical planning."""

    project_title: str
    estimated_time_minutes: int
    difficulty: str = "beginner"
    source_youtube_urls: list[str] = Field(default_factory=list)
    materials: list[MaterialItem] = Field(default_factory=list)
    steps: list[_WrittenStep] = Field(min_length=1)


class _GroundedStepUpdate(BaseModel):
    step_number: int
    visual_parent: int = Field(default=-1, ge=-1)
    physical_state: PhysicalStateTransition


class _HydrogenGroundingOutput(BaseModel):
    continuity_bible: VisualContinuityBible
    steps: list[_GroundedStepUpdate] = Field(default_factory=list)


async def synthesize_build_plan(
    goal: str,
    extracts: list[TutorialExtract],
    owned_items: list[str],
    gemini: Gemini4Client,
    *,
    on_activity: ActivityReporter | None = None,
) -> BuildPlan:
    """Write the guide once; send the footage planner only the actions and their observed contacts."""
    observations = [
        source.model_dump(include={"url", "extracted_materials", "segments", "common_mistakes"},
                          exclude_defaults=True) for source in extracts
    ]
    argon_prompt = (
        f"Goal: {goal}\nOwned items: {json.dumps(owned_items, ensure_ascii=False)}\n"
        f"Observed tutorials: {json.dumps(observations, ensure_ascii=False, separators=(',', ':'))}\n"
        "Choose one demonstrated method. Write only the operations needed for the requested result. "
        "Keep the guide complete, aim for 3–6 chapters on simple jobs. Each chapter demonstrates one "
        "continuous manipulation at a readable speed. Split different actions (folding, writing, opening) "
        "into distinct chapters. Skip optional decoration unless requested. For repeated identical actions, "
        "state the count and allow enough seconds to perform every repetition; never skip a required operation. "
        "For instruction_text, write one or two short sentences. Preserve measurements, real waiting times and necessary safety precautions. "
        "For narration_script, use one speakable action plus its stopping cue, usually 8–14 words. "
        "Number chapters from 1. Use 6 seconds for a simple motion, up to 15 when repetitions need it. "
        "List distinct materials and tools with short unique IDs, quantity and a simple shopping search. Cite source URLs used."
    )
    async with agent_operation(on_activity, id="plan-synthesis", stage="planning", label="Draft build steps",
                               agent="Build planner", model=gemini.model_id("argon") if on_activity else None,
                               detail=" · ".join(s.title for s in extracts)) as synthesis:
        draft = await gemini.generate_structured(
            role="argon", contents=argon_prompt, response_schema=_WrittenPlan, temperature=0.2,
            system_instruction="You write concise, actionable instructions grounded in observed craft demonstrations.",
        )
        source_urls = list(dict.fromkeys(s.url for s in extracts))
        cited = [url for url in dict.fromkeys(draft.source_youtube_urls) if url in source_urls] or source_urls
        owned_names = {" ".join(item.casefold().split()) for item in owned_items if item.strip()}
        for mat in draft.materials:
            name = " ".join(mat.name.casefold().split())
            mat.status = MaterialStatus.OWNED_EXACT if name in owned_names else MaterialStatus.BUY
            mat.substitute_note = None
        synthesis.detail = " · ".join(step.title for step in draft.steps)

    actions = [(s.step_number, s.title, s.instruction_text) for s in draft.steps]
    contacts = [f"{segment.technique}: {segment.hand_action}" for s in extracts if s.url in cited for segment in s.segments]
    hydrogen_prompt = (
        f"Guide: {draft.project_title}\nMaterials: {', '.join(m.name for m in draft.materials)}\n"
        f"Ordered chapter actions: {json.dumps(actions, ensure_ascii=False)}\n"
        f"Observed hand movements: {json.dumps(contacts, ensure_ascii=False)}\n"
        "For each numbered action give its starting geometry, one continuous manipulation, its resulting geometry, "
        "and one short, observable verification cue. Use one brief sentence per field. Name the fixed edge or support, "
        "moving part, contact point, direction of travel and stop. Hands cause the transformation, keep the work visible. "
        "For folds, say which layer lifts, where the tip lands and where fingers press the crease. "
        "For a repeated action, apply each repetition in sequence to the same piece. "
        "For a passive wait, state the actual wait and what changes. Preserve the same sheet of origami and every completed fold. "
        "visual_parent -1 continues the previous chapter; 0 starts a separate piece; a positive number resumes that earlier "
        "chapter's piece. Carry its end geometry into the next opening, with consistent orientation, surface, camera and hands. "
        "Choose a plain contrasting surface and paper finish where the contact point and crease are easy to see."
    )
    async with agent_operation(on_activity, id="physical-planning", stage="planning", label="Check physical continuity",
                               agent="Physical planner", model=gemini.model_id("hydrogen") if on_activity else None,
                               detail=" · ".join(s.title for s in draft.steps)) as physical:
        grounded = await gemini.generate_structured(
            role="hydrogen", contents=hydrogen_prompt, response_schema=_HydrogenGroundingOutput, temperature=0.15,
            system_instruction="Direct a single locked-camera, first-person craft demonstration with physically coherent chapters.",
        )
        updates = {s.step_number: s for s in grounded.steps}
        expected = {s.step_number for s in draft.steps}
        if len(grounded.steps) != len(draft.steps) or len(expected) != len(draft.steps) or updates.keys() != expected:
            raise ValueError("Grounding must provide exactly one physical transition per planned step.")
        steps = []
        for step in draft.steps:
            update = updates[step.step_number]
            steps.append(BuildStep(**step.model_dump(include=set(_WrittenStep.model_fields)),
                                   visual_parent=update.visual_parent, physical_state=update.physical_state,
                                   pov_video_prompt=update.physical_state.hand_kinematics))
        result = BuildPlan(project_title=draft.project_title, difficulty=draft.difficulty,
                           estimated_time_minutes=draft.estimated_time_minutes, source_youtube_urls=cited,
                           materials=draft.materials, steps=steps, continuity_bible=grounded.continuity_bible)
        physical.detail = f"Checked hand movements and physical dependencies across {len(steps)} steps."
        return result
