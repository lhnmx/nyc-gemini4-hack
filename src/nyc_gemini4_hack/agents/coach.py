from __future__ import annotations

import urllib.parse
from typing import Any

from google import genai
from google.genai import types

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.models import BuildPlan, CoachInspectResponse, CoachSessionResponse

LIVE_WS_BASE = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.{version}.GenerativeService.BidiGenerateContentConstrained"
)

COACH_TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "functionDeclarations": [
            {
                "name": "sync_current_step",
                "description": (
                    "Update the user's active step on screen when you can clearly see they have "
                    "moved to a different step in the build plan."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "step_number": {
                            "type": "INTEGER",
                            "description": "1-based step number from the build plan.",
                        },
                        "observation": {
                            "type": "STRING",
                            "description": "Very brief (under 12 words) summary of what you see them doing.",
                        },
                    },
                    "required": ["step_number"],
                },
            },
            {
                "name": "flag_mistake",
                "description": (
                    "Highlight a clear physical mistake or alignment/technique issue on screen while "
                    "briefly explaining how to fix it."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "step_number": {
                            "type": "INTEGER",
                            "description": "1-based step number where the mistake occurred.",
                        },
                        "issue": {
                            "type": "STRING",
                            "description": "Concise description of what looks wrong.",
                        },
                        "fix": {
                            "type": "STRING",
                            "description": "Concise, actionable correction using object-relative landmarks.",
                        },
                    },
                    "required": ["step_number", "issue", "fix"],
                },
            },
            {
                "name": "complete_step",
                "description": (
                    "Mark the current step complete when its expected end state is clearly visible "
                    "and advance the UI to the next step."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "step_number": {
                            "type": "INTEGER",
                            "description": "1-based step number that was just completed.",
                        },
                        "next_step_number": {
                            "type": "INTEGER",
                            "description": "1-based next step number to highlight, if any.",
                        },
                    },
                    "required": ["step_number"],
                },
            },
        ]
    }
]


def build_coach_system_instruction(plan: BuildPlan, current_step: int = 1) -> str:
    """Build a concise, quiet-by-default coaching prompt grounded in the BuildPlan."""
    step_numbers = [step.step_number for step in plan.steps]
    active = current_step if current_step in step_numbers else step_numbers[0]

    materials_text = ", ".join(
        f"{item.name} ({item.quantity})" for item in plan.materials
    ) or "None listed"

    step_blocks: list[str] = []
    for step in plan.steps:
        ps = step.physical_state
        cues = "; ".join(ps.verification_cues) if ps.verification_cues else "Match the expected end state."
        tips = "; ".join(step.tips) if step.tips else ""
        block = [
            f"Step {step.step_number}: {step.title}",
            f"  Instruction: {step.instruction_text}",
            f"  Start state: {ps.start_state}",
            f"  Hand movement: {ps.hand_kinematics}",
            f"  Expected end state: {ps.end_state}",
            f"  Checkpoints: {cues}",
        ]
        if tips:
            block.append(f"  Tips: {tips}")
        step_blocks.append("\n".join(block))

    steps_summary = "\n\n".join(step_blocks)

    return (
        "You are CraftPOV Live Coach — a calm, sharp hands-on craft mentor watching the user's "
        "front-facing laptop webcam and listening to their microphone in real time.\n\n"
        f"PROJECT: {plan.project_title} ({plan.difficulty})\n"
        f"MATERIALS: {materials_text}\n"
        f"STARTING STEP ON SCREEN: Step {active}\n\n"
        "FULL BUILD PLAN:\n"
        f"{steps_summary}\n\n"
        "CAMERA & VISUAL ACCURACY RULES:\n"
        "1. The user is using a normal laptop webcam (so the desk may be out of frame until they tilt down "
        "or hold their piece up). NEVER guess or pretend something looks right if you cannot clearly see it. "
        "If the workpiece is out of frame, blurry, or blocked by hands, briefly ask them to hold it up closer "
        "to the camera.\n"
        "2. Front-facing cameras mirror left and right. ALWAYS describe fixes using object-relative landmarks "
        "(e.g., 'the center crease', 'the pointed nose', 'the two top corners', 'the folded edge') instead of "
        "bare 'left' or 'right'.\n"
        "3. Compare what you see on the live webcam directly against the step's target visual reference image "
        "and checkpoints. Look closely at symmetry, fold direction, corner alignment, proportions, and step order.\n"
        "4. Be honest, direct, and fast — do not be overly polite or say 'looks great' when something is off. "
        "Name the exact visual mistake first, then give a 1-sentence fix.\n\n"
        "PACING (DO NOT BE CHATTY):\n"
        "1. Stay QUIET while the user is working normally or thinking. Never narrate every movement.\n"
        "2. Speak proactively ONLY when you spot a clear mistake (`flag_mistake` + brief 1–2 sentence fix) or "
        "when they clearly finish a step (`complete_step` + short 1-sentence cue for the next step). If you see "
        "them working on a different step than the one on screen, call `sync_current_step`.\n"
        "3. Whenever the user speaks to you, answer immediately in 1–3 concise, practical spoken sentences."
    )


async def inspect_workpiece_frame(
    plan: BuildPlan,
    step_number: int,
    frame_bytes: bytes,
    frame_mime: str,
    settings: Settings,
    reference_bytes: bytes | None = None,
    reference_mime: str = "image/jpeg",
    user_question: str = "",
) -> CoachInspectResponse:
    """High-precision physical inspection using Gemini 4 Hydrogen."""
    step = next((s for s in plan.steps if s.step_number == step_number), plan.steps[0])
    ps = step.physical_state
    cues = "\n".join(f"- {cue}" for cue in ps.verification_cues) or "- Matches expected end state cleanly."
    other_steps = "\n".join(
        f"- Step {s.step_number}: {s.title} -> End state: {s.physical_state.end_state}"
        for s in plan.steps
    )

    system_instruction = (
        "You are a strict, accurate physical craft inspector (Gemini 4 Hydrogen). "
        "Your job is to inspect the user's real workpiece in the webcam photo and catch real physical mistakes "
        "(wrong fold direction, asymmetric edges, misaligned corners, skipped creases, wrong proportions, "
        "loose joints, or wrong step).\n"
        "Rules:\n"
        "- Look closely at the actual geometry in the user's photo. Do NOT be polite or assume it is correct.\n"
        "- If the workpiece is not clearly visible, too blurry, too far, or blocked by hands, return verdict='UNCLEAR' "
        "and tell the user how to hold it up to the camera.\n"
        "- Only return verdict='PASS' if the expected end state and verification cues are visibly satisfied.\n"
        "- Return verdict='ADJUST' for small fixable alignment/symmetry/pressure issues, or 'REDO' if folded/shaped "
        "the wrong way or wrong orientation.\n"
        "- Because laptop webcams mirror left/right, describe fixes using object-relative landmarks "
        "(center crease, pointed tip, outer edge, top corners) instead of bare 'left'/'right'.\n"
        "- Keep what_i_see, mistakes, fix, and next_action concise and concrete."
    )

    prompt_parts: list[Any] = [
        f"PROJECT: {plan.project_title}\n"
        f"ACTIVE STEP: Step {step.step_number} — {step.title}\n"
        f"INSTRUCTION: {step.instruction_text}\n"
        f"EXPECTED START STATE: {ps.start_state}\n"
        f"EXPECTED HAND ACTION: {ps.hand_kinematics}\n"
        f"EXPECTED END STATE: {ps.end_state}\n"
        f"VERIFICATION CUES:\n{cues}\n\n"
        f"ALL STEPS FOR CONTEXT:\n{other_steps}\n"
        + (f"\nUSER QUESTION: {user_question}\n" if user_question.strip() else "")
    ]

    if reference_bytes:
        prompt_parts.append("TARGET REFERENCE IMAGE FOR THIS STEP (what it should look like):")
        prompt_parts.append(types.Part.from_bytes(data=reference_bytes, mime_type=reference_mime))

    prompt_parts.append("USER'S LIVE WEBCAM PHOTO (inspect this carefully):")
    prompt_parts.append(types.Part.from_bytes(data=frame_bytes, mime_type=frame_mime))

    gemini = Gemini4Client(settings)
    try:
        return await gemini.generate_structured(
            role="hydrogen",
            contents=prompt_parts,
            response_schema=CoachInspectResponse,
            system_instruction=system_instruction,
            temperature=0.1,
        )
    finally:
        await gemini.aclose()


async def create_live_coach_session(
    plan: BuildPlan,
    settings: Settings,
    current_step: int = 1,
) -> CoachSessionResponse:
    """Mint a single-use ephemeral Gemini Live API token for browser direct WebSocket connection."""
    if not settings.gemini_api_key.strip():
        raise RuntimeError("GEMINI_API_KEY is not set.")

    step_numbers = [step.step_number for step in plan.steps]
    initial_step = current_step if current_step in step_numbers else step_numbers[0]
    model_name = settings.live_coach_model
    if not model_name.startswith("models/"):
        model_name = f"models/{model_name}"

    client = genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(api_version=settings.live_coach_api_version),
    )
    try:
        token = await client.aio.auth_tokens.create(
            config=types.CreateAuthTokenConfig(uses=1),
        )
    finally:
        await client.aio.aclose()
        client.close()

    token_name = getattr(token, "name", "") or ""
    if not token_name:
        raise RuntimeError("Live coach token creation returned no token.")

    ws_base = LIVE_WS_BASE.format(version=settings.live_coach_api_version)
    ws_url = f"{ws_base}?access_token={urllib.parse.quote(token_name, safe='')}"

    return CoachSessionResponse(
        ws_url=ws_url,
        model=model_name,
        voice=settings.tts_voice,
        system_instruction=build_coach_system_instruction(plan, current_step=initial_step),
        tools=COACH_TOOL_DECLARATIONS,
        initial_step=initial_step,
    )
