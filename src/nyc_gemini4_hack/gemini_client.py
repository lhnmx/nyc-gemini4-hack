from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from nyc_gemini4_hack.config import ModelRole, Settings, get_settings
from nyc_gemini4_hack.models import DecisionResult

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class _DecisionPayload(BaseModel):
    choice: str
    confidence: float = Field(ge=0, le=1)
    rationale: str


class Gemini4Client:
    """Unified async-friendly wrapper for Gemini 4 (Argon, Carbon, Neon, Hydrogen) + Decisions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: genai.Client | None = None

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            if not self.settings.gemini_api_key.strip():
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Add your hackathon key to .env."
                )
            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client

    def model_id(self, role: ModelRole) -> str:
        return self.settings.model_for(role)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aio.aclose()
            finally:
                client.close()

    async def generate_text(
        self,
        role: ModelRole,
        contents: Any,
        system_instruction: str | None = None,
        temperature: float = 0.3,
    ) -> str:
        model = self.model_id(role)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
        )
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        return response.text or ""

    async def generate_structured(
        self,
        role: ModelRole,
        contents: Any,
        response_schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> T:
        model = self.model_id(role)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=response_schema,
        )
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        if response.parsed is not None:
            return response_schema.model_validate(response.parsed)
        if not response.text:
            raise ValueError(f"{model} returned no structured output.")
        return response_schema.model_validate_json(response.text)

    async def decide(
        self,
        gate_name: str,
        criteria: Mapping[str, str],
        instructions: str,
        context: str,
        role: ModelRole = "neon",
    ) -> DecisionResult:
        """
        Evaluate a structured choice gate using the native Gemini Decisions API
        (`client.decisions.create`), returning calibrated choice + confidence.
        """
        results = await self.decide_batch(
            context=context,
            questions={gate_name: (instructions, criteria)},
            role=role,
        )
        return results[gate_name]

    async def decide_batch(
        self,
        context: str,
        questions: Mapping[str, tuple[str, Mapping[str, str]]],
        role: ModelRole = "neon",
    ) -> dict[str, DecisionResult]:
        """
        Evaluate multiple structured choice questions against a shared context
        in a single native Decisions API forward pass.
        """
        model = self.model_id(role)
        decisions_model = self.settings.gemini_decisions_model
        if not questions:
            return {}
        results: dict[str, DecisionResult] = {}

        if self.settings.gemini_decisions_enabled and hasattr(self.client.aio, "decisions"):
            try:
                api_questions = {
                    q_id: {
                        "type": "choice",
                        "instructions": instr,
                        "criteria": dict(crit),
                    }
                    for q_id, (instr, crit) in questions.items()
                }
                resp = await self.client.aio.decisions.create(
                    model=decisions_model,
                    api_version=self.settings.gemini_decisions_api_version,
                    input=context,
                    questions=api_questions,
                )
            except Exception as exc:
                logger.warning(
                    "Native Decisions API (%s) failed (%s); falling back to structured %s.",
                    decisions_model,
                    type(exc).__name__,
                    model,
                )
            else:
                if getattr(getattr(resp, "prompt_feedback", None), "block_reason", None):
                    raise ValueError("The decision provider declined this input.")
                answers = getattr(resp, "answers", None) or {}
                for q_id, (_, criteria) in questions.items():
                    answer = answers.get(q_id)
                    if (
                        answer is None or answer.type != "choice"
                        or answer.choice not in criteria or not 0 <= answer.confidence <= 1
                    ):
                        continue
                    probabilities = getattr(answer, "probabilities", None) or {}
                    breakdown = ", ".join(
                        f"{option}={prob:.2f}" for option, prob in probabilities.items()
                    )
                    results[q_id] = DecisionResult(
                        gate_name=q_id, choice=answer.choice, confidence=answer.confidence,
                        rationale=f"Decisions API probabilities: {breakdown}" if breakdown else "Decisions API choice",
                        model_used=decisions_model,
                        via_decisions_api=True,
                    )

        # Reclassify only questions that the native call did not validly answer.
        for q_id, (instr, crit) in questions.items():
            if q_id in results:
                continue
            option_list = list(crit.keys())
            sys_instr = (
                "You are a deterministic decision gate. "
                f"Choose exactly one option from {option_list} based on these criteria: {dict(crit)}. "
                "Return JSON with choice, confidence (0..1), and a concise 1-sentence rationale."
            )
            payload = await self.generate_structured(
                role=role,
                contents=f"Context:\n{context}\n\nQuestion:\n{instr}",
                response_schema=_DecisionPayload,
                system_instruction=sys_instr,
                temperature=0.0,
            )
            if payload.choice not in crit:
                raise ValueError(f"The decision model returned an unknown choice for {q_id}.")
            results[q_id] = DecisionResult(
                gate_name=q_id,
                choice=payload.choice,
                confidence=payload.confidence,
                rationale=payload.rationale,
                model_used=model,
                via_decisions_api=False,
            )
        return results
