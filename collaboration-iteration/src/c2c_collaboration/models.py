"""Typed I/O models for the Collaboration & Iteration Agent (spec Inputs/Outputs)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReviewStatus = Literal[
    "in_review",  # staged, reviewers assigned, collecting feedback
    "in_revision",  # a round ran; a new version is staged for re-review
    "awaiting_conflict_resolution",  # contradictory feedback held for the Marketing Lead
    "content_confirmed",  # HUMAN-only terminal state
]

ItemType = Literal["textual", "structural", "out_of_scope"]
ItemOutcome = Literal[
    "applied", "deferred", "conflicted", "flagged_sourced_claim", "routed_structural",
    "logged_backlog", "rejected_by_human",
]


# ------------------------------------------------------------- feedback intake


class FeedbackItem(BaseModel):
    """One reviewer comment (dev: studio panel via the bridge; prod: Word comments
    via Graph behind the same FeedbackSource protocol). Internal-only content."""

    feedback_id: str
    campaign_id: str
    asset_id: str
    reviewer_id: str
    reviewer_role: str
    section: str = ""  # optional anchor; empty = whole asset
    text: str
    status: Literal["open", "consolidated"] = "open"
    round: int | None = None  # set when consumed by a round
    created_at: str = ""


# ------------------------------------------------------------- consolidation


class NormalizedItem(BaseModel):
    """One de-duplicated instruction with attribution and classification."""

    model_config = ConfigDict(extra="ignore")

    feedback_id: str
    location: str = ""
    instruction: str
    reviewer: str = ""
    type: ItemType = "textual"
    rationale: str = ""
    duplicate_of: str | None = None
    conflicts_with: str | None = None


class ConflictPosition(BaseModel):
    reviewer_id: str
    reviewer_role: str = ""
    quote: str  # the reviewer's own words — surfaced, never paraphrased away


class ConflictRecord(BaseModel):
    """Contradictory feedback: both positions quoted, section held. The agent
    NEVER picks a side — resolution carries the Marketing Lead's identity."""

    conflict_id: str
    campaign_id: str
    asset_id: str
    section: str
    positions: list[ConflictPosition]
    status: Literal["open", "resolved"] = "open"
    resolution: dict[str, str] | None = None  # decision, actor_id, actor_role, at
    round: int = 1
    created_at: str = ""


# ------------------------------------------------------------- revision round


class ItemResolution(BaseModel):
    feedback_id: str
    outcome: ItemOutcome
    note: str = ""


class ReviewRound(BaseModel):
    """One consolidation + revision pass. Every input feedback item appears in
    ``resolutions`` — items are never silently dropped (spec Fallback)."""

    campaign_id: str
    asset_id: str
    round: int
    input_feedback_ids: list[str] = Field(default_factory=list)
    normalized: list[NormalizedItem] = Field(default_factory=list)
    resolutions: list[ItemResolution] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)  # conflict_ids opened this round
    structural_instruction: str | None = None  # ONE consolidated rework instruction
    edit_summary: str = ""
    parent_version: int = 0
    new_version: int | None = None  # set when a revised draft was staged
    revised_ref: str | None = None
    marker_violations: list[str] = Field(default_factory=list)  # markers protected in code
    created_at: str = ""


# ------------------------------------------------------- assignment / state


class ReviewerAssignment(BaseModel):
    role: str
    focus: str


class ReviewState(BaseModel):
    """Per-asset review state machine (kind ``review_assignment``)."""

    campaign_id: str
    asset_id: str
    asset_type: str
    review_gate: str  # flagship | derivative
    reviewers: list[ReviewerAssignment] = Field(default_factory=list)
    due: str = ""  # ISO date from the workflow plan
    status: ReviewStatus = "in_review"
    rounds: int = 0
    draft_version: int = 0
    staged_at: str = ""
    confirmed_by: str | None = None
    confirmed_role: str | None = None
    confirmed_at: str | None = None
    reminders_sent: int = 0
    escalated: bool = False
    created_at: str = ""


class IterationMetrics(BaseModel):
    """Step 10: the raw material for sub-process 5's improvement loop."""

    campaign_id: str
    asset_id: str
    rounds: int
    feedback_items: int
    conflicts: int
    staged_at: str
    confirmed_at: str
    time_in_review_ms: int
    reminders_sent: int
    escalated: bool


# ------------------------------------------------------------- LLM contracts


class ConsolidationLLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[NormalizedItem] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RevisedSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str
    paragraphs: list[str] = Field(default_factory=list)


class RevisionLLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sections: list[RevisedSection] = Field(default_factory=list)
    applied: list[str] = Field(default_factory=list)  # feedback_ids
    deferred: list[dict[str, str]] = Field(default_factory=list)  # {feedback_id, reason}
    edit_summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ------------------------------------------------------------------- outcomes


class RoundOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    asset_id: str
    status: ReviewStatus
    round: ReviewRound | None = None
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)


class SweepOutcome(BaseModel):
    case_id: str
    trace_id: str
    reminded: list[str] = Field(default_factory=list)  # "campaign:asset"
    escalated: list[str] = Field(default_factory=list)
    checked: int = 0
