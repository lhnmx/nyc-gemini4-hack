from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ProjectStatus(StrEnum):
    QUEUED = "queued"
    RESEARCHING = "researching"
    PLANNING = "planning"
    GENERATING = "generating"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {self.COMPLETED, self.PARTIAL, self.FAILED}


class MaterialStatus(StrEnum):
    BUY = "BUY"
    OWNED_EXACT = "OWNED_EXACT"
    OWNED_SUBSTITUTE = "OWNED_SUBSTITUTE"


class CheckinVerdict(StrEnum):
    PASS = "PASS"
    ADJUST = "ADJUST"
    REDO = "REDO"


class DecisionResult(BaseModel):
    """Structured output returned by the Decisions API / gating layer."""

    gate_name: str
    choice: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str = ""
    model_used: str = ""
    via_decisions_api: bool = False


class TutorialSegment(BaseModel):
    timestamp_start: str
    timestamp_end: str
    technique: str
    hand_action: str
    tools_visible: list[str] = Field(default_factory=list)
    notes: str = ""


class TutorialExtract(BaseModel):
    """Structured multimodal extraction from a single YouTube video (Gemini 4 Carbon)."""

    video_id: str
    url: str
    title: str
    channel: str = ""
    language_or_audio_notes: str = ""
    extracted_materials: list[str] = Field(default_factory=list)
    segments: list[TutorialSegment] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)


class MaterialItem(BaseModel):
    id: str
    name: str
    category: str = "material"  # "material" | "tool"
    quantity: str = "1"
    required: bool = True
    search_query: str
    status: MaterialStatus = MaterialStatus.BUY
    substitute_note: str | None = None

    @field_validator("id")
    @classmethod
    def material_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Every material needs an ID.")
        return value.strip()


class VisualContinuityBible(BaseModel):
    workspace_surface: str
    lighting: str
    camera_style: str
    subject_palette: str
    hands_description: str = "clean maker hands, natural skin tone, no distracting jewelry"


class PhysicalStateTransition(BaseModel):
    start_state: str
    hand_kinematics: str
    end_state: str
    verification_cues: list[str] = Field(default_factory=list)


class BuildStep(BaseModel):
    step_number: int = Field(ge=1)
    title: str
    instruction_text: str
    narration_script: str
    pov_video_prompt: str
    physical_state: PhysicalStateTransition
    tips: list[str] = Field(default_factory=list)
    duration_seconds: int = Field(default=6, ge=1)

    @field_validator("narration_script")
    @classmethod
    def spoken_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Every step needs a narration script.")
        return value.strip()


class BuildPlan(BaseModel):
    project_title: str
    difficulty: str = "beginner"
    estimated_time_minutes: int = 30
    source_youtube_urls: list[str] = Field(default_factory=list)
    continuity_bible: VisualContinuityBible
    materials: list[MaterialItem] = Field(default_factory=list)
    steps: list[BuildStep] = Field(min_length=1)

    @field_validator("materials")
    @classmethod
    def distinct_materials(cls, materials: list[MaterialItem]) -> list[MaterialItem]:
        if len({material.id for material in materials}) != len(materials):
            raise ValueError("Every material needs a unique ID.")
        return materials

    @field_validator("steps")
    @classmethod
    def distinct_steps(cls, steps: list[BuildStep]) -> list[BuildStep]:
        if len({step.step_number for step in steps}) != len(steps):
            raise ValueError("Every build step needs a unique step number.")
        return steps


class GlobalProductOption(BaseModel):
    """Product option discovered across Shopify merchants via Global Catalog MCP."""

    material_id: str = ""
    material_name: str = ""
    product_id: str
    variant_id: str
    product_title: str
    merchant: str = ""
    price: str = "$0.00"
    price_minor: int = 0
    currency: str = "USD"
    product_url: str | None = None
    image_url: str | None = None
    available: bool = True


class CartLineItem(BaseModel):
    material_id: str
    material_name: str
    product_title: str
    variant_id: str
    price: str
    quantity: int = 1
    rationale: str | None = None
    product_url: str | None = None
    image_url: str | None = None


class ShoppingCartProposal(BaseModel):
    cart_id: str | None = None
    checkout_url: str | None = None
    store_domain: str | None = None
    subtotal: str = "$0.00"
    currency: str = "USD"
    matched_items: list[CartLineItem] = Field(default_factory=list)
    global_options: list[GlobalProductOption] = Field(default_factory=list)
    unmatched_materials: list[str] = Field(default_factory=list)


class StepMediaAsset(BaseModel):
    step_number: int
    status: str = "pending"  # pending, generating, ready, error or skipped
    media_mode: Literal["motion", "illustrated_frames"] | None = None
    keyframe_url: str | None = None
    video_url: str | None = None
    audio_url: str | None = None
    captions_url: str | None = None
    duration_seconds: float | None = Field(default=None, gt=0)
    chapter_start_seconds: float | None = Field(default=None, ge=0)
    error: str | None = None


class CheckinResponse(BaseModel):
    step_number: int
    verdict: CheckinVerdict
    feedback_text: str
    audio_url: str | None = None
    decision: DecisionResult | None = None


class ProjectEvent(BaseModel):
    event: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any] = Field(default_factory=dict)


class ProjectState(BaseModel):
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    goal: str
    owned_items: list[str] = Field(default_factory=list)
    desk_photo_url: str | None = None
    status: ProjectStatus = ProjectStatus.QUEUED
    status_message: str = "Queued"
    plan: BuildPlan | None = None
    cart: ShoppingCartProposal | None = None
    media: dict[int, StepMediaAsset] = Field(default_factory=dict)
    full_video_url: str | None = None
    full_captions_url: str | None = None
    decisions_log: list[DecisionResult] = Field(default_factory=list)
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CreateProjectRequest(BaseModel):
    goal: str = Field(..., min_length=2, max_length=300)
    owned_items: list[str] = Field(default_factory=list)


class RebuildCartRequest(BaseModel):
    owned_material_ids: list[str] = Field(default_factory=list)


class ShopifySearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=20)
    scope: str = Field(default="both")  # "global" | "store" | "both"


class ShopifyCartLineRequest(BaseModel):
    variant_id: str = Field(..., min_length=1)
    quantity: int = Field(default=1, ge=1, le=99)


class ShopifyCartRequest(BaseModel):
    items: list[ShopifyCartLineRequest] = Field(..., min_length=1)


class CoachSessionRequest(BaseModel):
    current_step: int = Field(default=1, ge=1)
    plan: BuildPlan | None = None


class CoachSessionResponse(BaseModel):
    ws_url: str
    model: str
    voice: str
    system_instruction: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    initial_step: int = Field(default=1, ge=1)


class CoachInspectResponse(BaseModel):
    step_number: int = Field(ge=1)
    verdict: Literal["PASS", "ADJUST", "REDO", "UNCLEAR"]
    detected_step_number: int = Field(ge=1)
    what_i_see: str
    mistakes: list[str] = Field(default_factory=list)
    fix: str = ""
    next_action: str = ""
