from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


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
    source_title: str | None = None
    source_url: str | None = None


class ObservedTechnique(BaseModel):
    technique: str
    hand_action: str
    tools_visible: list[str] = Field(default_factory=list)
    notes: str = ""


def _timestamp_seconds(timestamp: str) -> int | None:
    match = re.fullmatch(r"(?:([0-9]{1,3}):([0-5][0-9])|([0-9]{1,4})):([0-5][0-9])", timestamp.strip())
    if not match:
        return None
    hours, minutes, total_minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or total_minutes) * 60 + int(seconds)


class TutorialSegment(ObservedTechnique):
    timestamp_start: str
    timestamp_end: str

    @property
    def time_range_seconds(self) -> tuple[int, int] | None:
        start = _timestamp_seconds(self.timestamp_start)
        end = _timestamp_seconds(self.timestamp_end)
        if start is None or end is None or end <= start:
            return None
        return start, end


class StepSourceSegment(ObservedTechnique):
    """A chapter observation resolved from the footage analyst's timestamped extract."""

    segment_id: str
    source_url: str
    source_title: str
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def forward_interval(self) -> StepSourceSegment:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("A source segment must end after it starts.")
        return self


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

    def source_observations(self, prefix: str) -> list[StepSourceSegment]:
        observations = []
        for number, segment in enumerate(self.segments, start=1):
            window = segment.time_range_seconds
            if window and segment.hand_action.strip():
                observations.append(StepSourceSegment(
                    segment_id=f"{prefix}.{number}", source_url=self.url, source_title=self.title,
                    start_seconds=window[0], end_seconds=window[1],
                    **segment.model_dump(include=set(ObservedTechnique.model_fields)),
                ))
        return observations


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
    visual_parent: int = Field(
        default=-1, ge=-1,
        description="Use -1 to continue the preceding step, 0 for an independent setup, or an earlier step number.",
    )
    title: str
    instruction_text: str
    narration_script: str
    pov_video_prompt: str
    physical_state: PhysicalStateTransition
    source_segments: list[StepSourceSegment] = Field(default_factory=list)
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
        earlier: set[int] = set()
        for step in steps:
            if step.step_number in earlier:
                raise ValueError("Every build step needs a unique step number.")
            if step.visual_parent > 0 and step.visual_parent not in earlier:
                raise ValueError("A visual parent must refer to an earlier step in this guide.")
            earlier.add(step.step_number)
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


class FrameFinding(BaseModel):
    verdict: Literal["matches", "contradicts", "unclear"]
    sample: int = Field(ge=1, description="One-based index of the observed frame in the sample sequence.")
    observation: str = Field(min_length=1)


class VisualReview(BaseModel):
    status: Literal["passed", "rejected", "uncertain", "unavailable"]
    model: str
    source_segment_ids: list[str] = Field(default_factory=list)
    opening: FrameFinding | None = None
    contact: FrameFinding | None = None
    result: FrameFinding | None = None
    review_seconds: float = Field(ge=0)
    unavailable_reason: str | None = None


class ShotReview(VisualReview):
    attempt: int = Field(ge=1, le=2)
    sampled_seconds: list[float] = Field(default_factory=list)
    reference_mode: Literal["text", "first_frame", "first_last_frames"]
    target_origin: Literal["reviewed_render", "reviewed_illustration"] | None = None
    target_image_model: str | None = None
    target_preparation_seconds: float | None = Field(default=None, ge=0)
    render_seconds: float = Field(ge=0)


class StepMediaAsset(BaseModel):
    step_number: int
    status: str = "pending"  # pending, generating, ready, error or skipped
    media_mode: Literal["motion", "illustrated_frames"] | None = None
    video_model: str | None = None
    shot_reviews: list[ShotReview] = Field(default_factory=list)
    motion_failure: str | None = None
    illustration_review: VisualReview | None = None
    voice_model: str | None = None
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


class ResearchSource(BaseModel):
    gate_name: str
    title: str
    url: str
    channel: str = ""
    duration_seconds: int | None = Field(default=None, ge=0)


class AgentActivity(BaseModel):
    id: str
    stage: Literal["research", "planning", "motion", "illustration", "narration", "assembly"]
    state: Literal["started", "completed", "failed"]
    label: str
    agent: str
    model: str | None = None
    detail: str = ""
    sources: list[ResearchSource] = Field(default_factory=list)
    source_segments: list[StepSourceSegment] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    step_number: int | None = Field(default=None, ge=1)


class MediaProgress(BaseModel):
    stage: Literal["footage", "assembly", "stitching"]
    step_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    step_number: int | None = Field(default=None, ge=1)


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
    media_progress: MediaProgress | None = None
    activity: list[AgentActivity] = Field(default_factory=list)
    full_video_url: str | None = None
    full_captions_url: str | None = None
    decisions_log: list[DecisionResult] = Field(default_factory=list)
    error: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def record_activity(self, entry: AgentActivity) -> None:
        for index, current in enumerate(self.activity):
            if current.id == entry.id:
                self.activity[index] = entry
                return
        self.activity.append(entry)


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
