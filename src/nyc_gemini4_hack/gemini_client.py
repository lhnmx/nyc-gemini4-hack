from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping, Sequence, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from nyc_gemini4_hack.config import ModelRole, Settings, get_settings
from nyc_gemini4_hack.models import DecisionResult

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class _DecisionPayload(BaseModel):
    choice: str
    confidence: float
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
        response = await asyncio.to_thread(
            self.client.models.generate_content,
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
        response = await asyncio.to_thread(
            self.client.models.generate_content,
            model=model,
            contents=contents,
            config=config,
        )
        if getattr(response, "parsed", None) is not None:
            return response.parsed  # type: ignore[return-value]
        raw_text = response.text or "{}"
        return response_schema.model_validate(json.loads(raw_text))

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

        if self.settings.gemini_decisions_enabled and hasattr(self.client, "decisions"):
            try:
                api_questions = {
                    q_id: {
                        "type": "choice",
                        "instructions": instr,
                        "criteria": dict(crit),
                    }
                    for q_id, (instr, crit) in questions.items()
                }
                resp = await asyncio.to_thread(
                    self.client.decisions.create,
                    model=decisions_model,
                    api_version=self.settings.gemini_decisions_api_version,
                    input=context,
                    questions=api_questions,
                )
                out: dict[str, DecisionResult] = {}
                answers = resp.answers or {}
                for q_id, (_, crit) in questions.items():
                    ans = answers.get(q_id)
                    if ans is not None and hasattr(ans, "choice"):
                        probs = getattr(ans, "probabilities", {}) or {}
                        prob_summary = ", ".join(
                            f"{k}={v:.2f}" for k, v in sorted(probs.items(), key=lambda x: -x[1])
                        )
                        out[q_id] = DecisionResult(
                            gate_name=q_id,
                            choice=str(ans.choice),
                            confidence=float(getattr(ans, "confidence", 1.0)),
                            rationale=f"Decisions API calibrated probs: {prob_summary}"
                            if prob_summary
                            else "Decisions API choice",
                            model_used=decisions_model,
                            via_decisions_api=True,
                        )
                    else:
                        first_opt = next(iter(crit.keys()))
                        out[q_id] = DecisionResult(
                            gate_name=q_id,
                            choice=first_opt,
                            confidence=0.5,
                            rationale="Decisions API returned no answer; defaulted.",
                            model_used=decisions_model,
                            via_decisions_api=True,
                        )
                return out
            except Exception as exc:
                logger.warning(
                    "Native Decisions API (%s) failed (%s); falling back to structured %s.",
                    decisions_model,
                    exc,
                    model,
                )

        # Fallback: structured constrained classification on Gemini 4 Neon
        out_fallback: dict[str, DecisionResult] = {}
        for q_id, (instr, crit) in questions.items():
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
            choice = payload.choice if payload.choice in option_list else option_list[0]
            out_fallback[q_id] = DecisionResult(
                gate_name=q_id,
                choice=choice,
                confidence=max(0.0, min(1.0, payload.confidence)),
                rationale=payload.rationale,
                model_used=model,
                via_decisions_api=False,
            )
        return out_fallback
