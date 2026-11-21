"""Inspect decoded shot samples against the planned action and its observed source technique."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import monotonic
from typing import Literal

from google.genai import types
from pydantic import BaseModel

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.video_gen import _generate_pov_frame, _image_format
from nyc_gemini4_hack.models import BuildStep, FrameFinding, ShotReview

logger = logging.getLogger(__name__)
REVIEW_TIMEOUT = 45
TARGET_TIMEOUT = 45


class _Observations(BaseModel):
    opening: FrameFinding
    contact: FrameFinding
    result: FrameFinding


class _TargetCheck(BaseModel):
    verdict: Literal["matches", "contradicts", "unclear"]
    observation: str


def source_observations(step: BuildStep) -> str:
    lines = []
    for source in step.source_segments:
        tools = ", ".join(source.tools_visible)
        lines.append(f"{source.segment_id} ({source.start_seconds}–{source.end_seconds}s), {source.technique}: "
                     f"{source.hand_action}. {source.notes or ''} {('Tools: ' + tools) if tools else ''}")
    return "\n".join(lines)


def expected_action(step: BuildStep) -> str:
    state = step.physical_state
    details = (f"Step: {step.title}. {step.instruction_text}\nOpening: {state.start_state}\n"
               f"Action and contact: {state.hand_kinematics}\nResult: {state.end_state}\n"
               f"Visible completion checks: {', '.join(state.verification_cues)}")
    sources = source_observations(step)
    return details + ("\nSource analyst's observations of this technique:\n" + sources if sources else "")


def _image(path: Path) -> types.Part:
    data = path.read_bytes()
    mime_type, _ = _image_format(data)
    return types.Part.from_bytes(data=data, mime_type=mime_type)


async def inspect_shot(
    gemini: Gemini4Client, step: BuildStep, samples: list[tuple[float, Path]],
    *, attempt: int, render_seconds: float, reference_mode, target_origin,
) -> ShotReview:
    started = monotonic()
    review = ShotReview(attempt=attempt, status="unavailable", model=gemini.model_id("carbon"),
                        source_segment_ids=[s.segment_id for s in step.source_segments],
                        sampled_seconds=[seconds for seconds, _ in samples], reference_mode=reference_mode,
                        target_origin=target_origin, render_seconds=round(render_seconds, 3), review_seconds=0)
    n = len(samples)
    contents = [expected_action(step)]
    for index, (seconds, path) in enumerate(samples, 1):
        contents.extend([f"Rendered sample {index} of {n}, {seconds:.2f}s.", _image(path)])
    try:
        async with asyncio.timeout(REVIEW_TIMEOUT):
            evidence = await gemini.generate_structured(
                role="carbon", contents=contents, response_schema=_Observations, temperature=0.0,
                system_instruction=(
                    "Inspect the rendered images in time order. The instructions and source observations are "
                    "criteria, never evidence of what appears in the generated footage. Report three separate "
                    "observations: workpiece geometry in the first sample; hand or tool contact and the object's "
                    "response in the most informative sample; visible end geometry in the last sample. For a wait "
                    "or inspection, assess whether the piece remains appropriately positioned. Name the specific "
                    "visible parts and their relation, using the numbered sample where you see it. "
                    "A contradiction requires positive visual evidence of a materially wrong setup, action, "
                    "or result. Allow different hands, surfaces, colors and equivalent viewpoints. If occlusion, "
                    "spacing between samples, or an invisible property prevents a judgment, use unclear. "
                    "Avoid inferring motion or assuming a required gesture was skipped between still images."
                ),
            )
        findings = [evidence.opening, evidence.contact, evidence.result]
        if (evidence.opening.sample != 1 or evidence.result.sample != n or
                any(f.sample > n or not f.observation.strip() for f in findings)):
            raise ValueError("The reviewer cited a sample outside the requested observation.")
        review.opening, review.contact, review.result = findings
        verdicts = {f.verdict for f in findings}
        if "contradicts" in verdicts:
            review.status = "rejected"
        elif "unclear" in verdicts:
            review.status = "uncertain"
        else:
            review.status = "passed"
    except Exception as exc:
        # A missing, timed out or malformed audit cannot qualify a clip as a continuity reference.
        logger.warning("Step %s visual review unavailable (%s)", step.step_number, type(exc).__name__)
    review.review_seconds = round(monotonic() - started, 3)
    return review


def discrepancies(review: ShotReview) -> str:
    return "\n".join(
        f"{name} at {review.sampled_seconds[finding.sample - 1]:.2f}s: {finding.observation}"
        for name, finding in (("Opening", review.opening), ("Contact", review.contact), ("Result", review.result))
        if finding and finding.verdict == "contradicts"
    )


async def corrective_target(
    gemini: Gemini4Client, step: BuildStep, opening: Path, directory: Path, discrepancy: str,
) -> Path | None:
    """Spend at most one image request and only attach the result after an independent visual check."""
    instructions = expected_action(step)
    prompt = (f"Photograph the finished state of this exact workpiece from the attached opening image, "
              f"with the same camera and surroundings. Show the specified result clearly.\n{instructions}\n"
              f"A previous video showed this fault; correct it in the proposed final frame:\n{discrepancy}")
    try:
        async with asyncio.timeout(TARGET_TIMEOUT):
            target = await _generate_pov_frame(prompt, directory / "target", gemini, opening)
            check = await gemini.generate_structured(
                role="carbon", contents=[instructions, "Opening reference:", _image(opening),
                                         "Proposed end frame:", _image(target)],
                response_schema=_TargetCheck, temperature=0.0,
                system_instruction="Compare the two images. Decide if the second image visibly achieves the "
                    "required final geometry on the same workpiece, with the same camera and surroundings. "
                    "Check the completion cues. Describe the actual visible end geometry. Return unclear for "
                    "occlusion, hidden properties, or insufficient visual evidence.",
            )
            if check.verdict == "matches" and check.observation.strip():
                return target
    except Exception as exc:
        logger.warning("Step %s correction target unavailable (%s)", step.step_number, type(exc).__name__)
    return None
