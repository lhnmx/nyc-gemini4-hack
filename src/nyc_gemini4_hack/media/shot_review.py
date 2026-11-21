"""Inspect decoded shot samples against the planned action and its observed source technique."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from time import monotonic
from typing import Literal

import httpx
import imageio_ffmpeg
from google.genai import types
from pydantic import BaseModel

from nyc_gemini4_hack.gemini_client import Gemini4Client
from nyc_gemini4_hack.media.composer import run_media_work
from nyc_gemini4_hack.media.video_gen import _generate_pov_frame, _image_format, source_observations
from nyc_gemini4_hack.models import BuildStep, FrameFinding, ShotReview, VisualReview

logger = logging.getLogger(__name__)
REVIEW_TIMEOUT = 45
ILLUSTRATION_REVIEW_RETRY_TIMEOUT = 90
TARGET_TIMEOUT = 45


class _Observations(BaseModel):
    opening: FrameFinding
    contact: FrameFinding
    result: FrameFinding


class _TargetCheck(BaseModel):
    verdict: Literal["matches", "contradicts", "unclear"]
    observation: str


def expected_action(step: BuildStep) -> str:
    state = step.physical_state
    details = (f"Step: {step.title}. {step.instruction_text}\nOpening: {state.start_state}\n"
               f"Action and contact: {state.hand_kinematics}\nResult: {state.end_state}\n"
               f"Visible completion checks: {', '.join(state.verification_cues)}")
    sources = source_observations(step)
    return details + ("\nSource analyst's observations of this technique:\n" + sources if sources else "")


def _image(path: Path, compact: bool = False) -> types.Part:
    if compact and path.stat().st_size > 512 * 1024:
        # The reviewed sequence is published at 720p. Send those details without uploading
        # multi-megabyte lossless image responses on each review request.
        data = subprocess.run([
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-loglevel", "error", "-xerror", "-i", str(path),
            "-vf", "scale='min(iw,1280)':'min(ih,720)':force_original_aspect_ratio=decrease",
            "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "2", "-f", "image2pipe", "pipe:1",
        ], capture_output=True, check=True, timeout=20).stdout
    else:
        data = path.read_bytes()
    mime_type, _ = _image_format(data)
    return types.Part.from_bytes(data=data, mime_type=mime_type)


async def inspect_shot(
    gemini: Gemini4Client, step: BuildStep, samples: list[tuple[float, Path]],
    *, attempt: int, render_seconds: float, reference_mode, target_origin,
) -> ShotReview:
    review = ShotReview(attempt=attempt, status="unavailable", model=gemini.model_id("carbon"),
                        source_segment_ids=[s.segment_id for s in step.source_segments],
                        sampled_seconds=[seconds for seconds, _ in samples], reference_mode=reference_mode,
                        target_origin=target_origin, render_seconds=round(render_seconds, 3), review_seconds=0)
    labelled = [(f"Rendered sample {i} of {len(samples)}, {seconds:.2f}s.", path)
                for i, (seconds, path) in enumerate(samples, 1)]
    await _inspect(gemini, step, review, labelled)
    return review


async def inspect_illustrations(gemini: Gemini4Client, step: BuildStep, frames: list[Path]) -> VisualReview:
    review = VisualReview(status="unavailable", model=gemini.model_id("carbon"),
                          source_segment_ids=[s.segment_id for s in step.source_segments], review_seconds=0)
    if len(frames) == 3:
        labels = ["Illustration 1: opening.", "Illustration 2: action at contact.", "Illustration 3: result."]
        await _inspect(gemini, step, review, list(zip(labels, frames, strict=True)), illustrations=True)
    else:
        review.unavailable_reason = "The illustration sequence was incomplete."
    return review


async def _inspect(gemini: Gemini4Client, step: BuildStep, review: VisualReview, labelled: list[tuple[str, Path]],
                   *, illustrations: bool = False) -> None:
    started = monotonic()
    n = len(labelled)
    guidance = (" These are three teaching illustrations. Inspect the working grip, contact, or resting setup "
                "in illustration 2. Mark the contact unclear if this still cannot teach the required technique."
                if illustrations else "")
    contents = [expected_action(step)]
    instruction = (
        "Inspect the rendered images in time order. The instructions and source observations are "
        "criteria, never evidence of what appears in the generated footage. Report three separate "
        "observations: workpiece geometry in the first sample; hand or tool contact and the object's "
        "response in the most informative sample; visible end geometry in the last sample. For a wait "
        "or inspection, assess whether the piece remains appropriately positioned. Name the specific "
        "visible parts and their relation, using the numbered sample where you see it. "
        "A contradiction requires positive visual evidence of a materially wrong setup, action, "
        "or result. Allow different hands, surfaces, colors and equivalent viewpoints. If occlusion, "
        "spacing between samples, or an invisible property prevents a judgment, use unclear. "
        "Avoid inferring motion or assuming a required gesture was skipped between still images." + guidance
    )
    try:
        for label, path in labelled:
            image = await run_media_work(_image, path, True) if illustrations else _image(path)
            contents.extend([label, image])
        deadlines = (REVIEW_TIMEOUT, ILLUSTRATION_REVIEW_RETRY_TIMEOUT) if illustrations else (REVIEW_TIMEOUT,)
        for attempt, deadline in enumerate(deadlines, 1):
            try:
                async with asyncio.timeout(deadline):
                    evidence = await gemini.generate_structured(
                        role="carbon", contents=contents, response_schema=_Observations, temperature=0.0,
                        system_instruction=instruction,
                    )
                break
            except Exception as exc:
                code = getattr(exc, "code", None)
                transient = isinstance(exc, (TimeoutError, httpx.TransportError)) or code in {408, 429, 500, 502, 503, 504}
                if attempt == len(deadlines) or not transient:
                    raise
                logger.info("Step %s retrying the illustration review on the same frames (%s)",
                            step.step_number, type(exc).__name__)
                await asyncio.sleep(1)
        findings = [evidence.opening, evidence.contact, evidence.result]
        if (evidence.opening.sample != 1 or evidence.result.sample != n or
                (illustrations and evidence.contact.sample != 2) or
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
        code = getattr(exc, "code", None)
        if isinstance(code, int) and 400 <= code <= 599:
            review.unavailable_reason = f"The review provider returned HTTP {code}."
        elif isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            review.unavailable_reason = "The review request timed out."
        elif isinstance(exc, httpx.TransportError):
            review.unavailable_reason = "The review provider could not be reached."
        elif isinstance(exc, ValueError):
            review.unavailable_reason = "The reviewer did not return valid frame observations."
        logger.warning("Step %s visual review unavailable (%s)", step.step_number, type(exc).__name__)
    review.review_seconds = round(monotonic() - started, 3)


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
