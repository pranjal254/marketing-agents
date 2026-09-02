"""Typed I/O models for the Campaign Identification Agent (spec Inputs/Outputs)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RequestSource = Literal["form", "plan", "calendar", "adhoc"]
CaseStatus = Literal[
    "intake",
    "awaiting_input",
    "draft_review",  # brief drafted, held with the requester for verification (not yet routed)
    "awaiting_approval",
    "approved",
    "rejected",
    "escalated",
    "failed",
]
CampaignType = Literal["demand_gen", "offering_launch_support", "event_follow_up"]
Priority = Literal["high", "medium", "low"]
Freshness = Literal["fresh", "stale"]


class CampaignRequest(BaseModel):
    """Common request record — all three entry points normalize into this (Task 1)."""

    model_config = ConfigDict(extra="forbid")

    request_id: str
    source: RequestSource
    received_at: str
    requester: str | None = None
    objective: str | None = None
    business_unit: str | None = None
    vertical: str | None = None
    target_segment: str | None = None
    offer_topic: str | None = None
    channels: list[str] = Field(default_factory=list)
    timeline_start: str | None = None
    timeline_end: str | None = None
    owner: str | None = None
    budget_flag: bool | None = None
    products: list[str] = Field(default_factory=list)
    free_text_context: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    # fields filled by the extraction step from the requester's own words:
    # field name -> the supporting quote/source note (provenance, never invention)
    derived_fields: dict[str, str] = Field(default_factory=dict)


class MissingField(BaseModel):
    field: str
    code: str  # e.g. missing_objective / ambiguous_vertical
    kind: Literal["missing", "ambiguous"]
    detail: str | None = None


class ValidationResult(BaseModel):
    missing: list[MissingField] = Field(default_factory=list)
    complete: bool
    completeness_score: float = Field(ge=0.0, le=1.0)


class BcFoCheck(BaseModel):
    mixed: bool
    evidence: list[str] = Field(default_factory=list)
    split_proposal: list[str] = Field(default_factory=list)


class ConflictFlag(BaseModel):
    kind: Literal["duplicate", "timing"]
    conflicting_campaign_id: str
    rationale: str
    freshness: Freshness


class Classification(BaseModel):
    campaign_type: CampaignType
    priority: Priority
    channel_mix: list[str]
    segment_relevance: str
    field_rationale: dict[str, str] = Field(default_factory=dict)  # field -> named source


class BriefField(BaseModel):
    name: str
    value: str
    provenance: str  # every populated field carries its source (guardrail 1)


class CampaignBrief(BaseModel):
    campaign_id: str
    case_id: str
    version: int
    status: CaseStatus
    fields: list[BriefField]
    classification: Classification | None = None
    conflicts: list[ConflictFlag] = Field(default_factory=list)
    bc_fo: BcFoCheck | None = None
    template_version: str
    created_at: str


class GapQuestion(BaseModel):
    field: str
    question: str


class GapRequest(BaseModel):
    case_id: str
    round: int
    questions: list[GapQuestion]
    sent_to: str
    created_at: str


class ApprovalRecord(BaseModel):
    decision: Literal["approved", "rejected", "modified"]
    actor_role: str
    actor_id: str
    timestamp: str
    notes: str | None = None


class IntakeContext(BaseModel):
    """Compact intake summary persisted to the Context Store (Task 9)."""

    case_id: str
    campaign_id: str | None
    request_source: RequestSource
    status: CaseStatus
    classification: Classification | None
    conflicts: list[ConflictFlag]
    validation: ValidationResult
    approval: ApprovalRecord | None
    gap_rounds: int


class ClassifyOutput(BaseModel):
    """Layer 3 JSON contract (parsed, validated LLM output)."""

    model_config = ConfigDict(extra="ignore")

    action_class: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str
    classification: Classification | None = None
    normalized_fields: dict[str, str] = Field(default_factory=dict)


class ProcessOutcome(BaseModel):
    case_id: str
    trace_id: str
    status: CaseStatus
    action_class: str | None
    brief: CampaignBrief | None = None
    gap_request: GapRequest | None = None
    escalation_reason: str | None = None
    doc_ref: str | None = None
