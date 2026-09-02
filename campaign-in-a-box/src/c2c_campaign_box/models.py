"""Typed I/O models for the Campaign-in-a-Box Orchestrator (spec Inputs/Outputs)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PlanStatus = Literal[
    "planning",
    "awaiting_confirmation",  # pack + plan routed to the Marketing Lead (step 8)
    "confirmed",  # pack AND plan confirmed — assets move to production
    "in_production",
    "packaging_blocked",  # non-empty completeness diff or gate violation
    "packaged_pending_compliance",  # manifest registered, handed to the Quality Gate
    "escalated",
    "failed",
]

AssetStatus = Literal["planned", "in_production", "content_confirmed", "packaged", "reopened"]
IntelMode = Literal["semrush_plus_library", "intel_library_only"]
ReuseDecision = Literal["reuse", "adapt", "create"]
FunnelStage = Literal["awareness", "consideration", "decision", "expansion"]


# ------------------------------------------------------------------- intel (step 2)


class IntelSignal(BaseModel):
    """One sourced data point gathered before the planning pass. Every signal has a
    source URI and retrieval timestamp — spec: record provenance for every data point."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str
    origin: Literal["semrush", "intel_library"]
    kind: Literal["keyword", "related_keyword", "organic_result", "file"]
    summary: str
    source_uri: str
    retrieved_at: str
    data: dict[str, Any] = Field(default_factory=dict)


class IntelBundle(BaseModel):
    topic: str
    mode: IntelMode
    signals: list[IntelSignal] = Field(default_factory=list)
    semrush_failure: str | None = None  # why fallback engaged (flagged, never silent)


# ------------------------------------------------- audience & offer pack (steps 3-4)


class ProofPoint(BaseModel):
    claim: str
    source_ref: str  # must resolve to a gathered signal URI or a brief field
    status: Literal["verified", "unverified"] = "verified"


class PersonaProfile(BaseModel):
    persona_id: str
    title: str
    role_pains: str
    rationale: str  # grounding: brief field / signal reference


class MessagingAngle(BaseModel):
    persona_id: str
    angle: str
    grounding: str


class AudienceOfferPack(BaseModel):
    campaign_id: str
    version: int
    vertical: str
    segment_applicability: dict[str, str] = Field(default_factory=dict)  # type_3/type_4 → rationale
    personas: list[PersonaProfile] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    value_proposition: str = ""
    differentiators: list[str] = Field(default_factory=list)
    proof_points: list[ProofPoint] = Field(default_factory=list)
    ctas: dict[str, str] = Field(default_factory=dict)  # funnel stage → CTA
    messaging_angles: list[MessagingAngle] = Field(default_factory=list)
    channel_emphasis: dict[str, str] = Field(default_factory=dict)  # channel → grounded rationale
    gaps: list[str] = Field(default_factory=list)  # explicit gaps, never plausible filler
    intel_mode: IntelMode = "intel_library_only"
    unverified_share: float = 0.0
    lint_findings: list[dict[str, str]] = Field(default_factory=list)
    template_version: str = "0.1.0-draft"
    created_at: str = ""


# ------------------------------------------------------- asset checklist (step 5)


class RepoCandidate(BaseModel):
    asset_ref: str  # repository URI / path (read-only source)
    title: str
    asset_type: str | None = None
    vertical: str | None = None
    business_unit: str | None = None
    modified_at: str | None = None
    fitness_score: float = Field(ge=0.0, le=1.0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)


class AssetChecklistItem(BaseModel):
    asset_id: str
    asset_type: str
    label: str
    volume: int = 1
    decision: ReuseDecision
    decision_rationale: str
    candidates_evaluated: list[RepoCandidate] = Field(default_factory=list)
    reuse_ref: str | None = None  # set only for reuse/adapt, must be an evaluated candidate
    reuse_check_pending: bool = False  # repository unavailable → create + pending, flagged
    status: AssetStatus = "planned"


class AssetChecklist(BaseModel):
    campaign_id: str
    version: int
    items: list[AssetChecklistItem]
    search_performed: bool
    created_at: str = ""


# ------------------------------------------------------------- outlines (step 6)


class OutlineSection(BaseModel):
    heading: str
    notes: str
    planned_claims: list[str] = Field(default_factory=list)  # proof-point source_refs


class ContentOutline(BaseModel):
    asset_id: str
    asset_type: str
    title: str
    sections: list[OutlineSection] = Field(default_factory=list)
    seeded_from_angles: list[str] = Field(default_factory=list)  # persona_ids of angles used


# ------------------------------------------------- calendar & workflow plan (step 7)


class ScheduleEntry(BaseModel):
    asset_id: str
    asset_type: str
    draft_due: str  # ISO dates
    review_due: str
    confirm_due: str
    review_gate: Literal["flagship", "derivative"]
    constraint_chain: str  # explainability: why this date (back-planning chain)


class InfeasibilityReport(BaseModel):
    reasons: list[str]
    trade_offs: list[str]  # explicit options — never silently compress review gates


class WorkflowPlan(BaseModel):
    campaign_id: str
    version: int
    window_start: str
    window_end: str
    entries: list[ScheduleEntry] = Field(default_factory=list)
    feasible: bool = True
    infeasibility: InfeasibilityReport | None = None
    capacity_note: str = ""
    created_at: str = ""


# ------------------------------------------------------- confirmation gate (step 8)


class ConfirmationRecord(BaseModel):
    kind: Literal["pack", "plan", "asset_content"]
    decision: Literal["confirmed", "modified", "rejected"]
    actor_id: str
    actor_role: str
    timestamp: str
    deltas: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


# ------------------------------------------------------ packaging module (steps 9-12)


class RegisteredAsset(BaseModel):
    """A produced asset registered in the Context Store (in prod, by Agents 3-4;
    in dev, by the bridge's stand-in endpoint)."""

    asset_id: str
    asset_type: str
    filename: str
    file_ref: str  # workspace ref of the confirmed version
    version: int
    status: AssetStatus
    confirmation: ConfirmationRecord | None = None  # human content-confirmed record
    claim_refs: list[str] = Field(default_factory=list)  # claim-lineage index input


class CompletenessDiff(BaseModel):
    missing: list[str] = Field(default_factory=list)  # checklist asset_ids not registered
    extra: list[str] = Field(default_factory=list)  # registered but not on the checklist
    version_mismatch: list[str] = Field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.missing or self.extra or self.version_mismatch)


class NamingIssue(BaseModel):
    asset_id: str
    given: str
    expected: str
    resolution: Literal["auto_corrected", "flagged"]


class PackagedAsset(BaseModel):
    asset_id: str
    asset_type: str
    canonical_name: str
    source_ref: str
    snapshot_ref: str
    version: int
    sha256: str


class PackageManifest(BaseModel):
    manifest_id: str
    campaign_id: str
    version: int
    status: Literal["packaged_pending_compliance"] = "packaged_pending_compliance"
    assets: list[PackagedAsset] = Field(default_factory=list)
    calendar_ref: str = ""
    checklist_version: int = 0
    claim_lineage_index: dict[str, list[str]] = Field(default_factory=dict)
    naming_corrections: list[NamingIssue] = Field(default_factory=list)
    created_at: str = ""


class CompletenessReport(BaseModel):
    campaign_id: str
    diff: CompletenessDiff
    missing_confirmations: list[str] = Field(default_factory=list)
    naming_flags: list[NamingIssue] = Field(default_factory=list)
    owners_note: str = ""
    created_at: str = ""


# ------------------------------------------------------------- LLM output contracts


class PackLLMOutput(BaseModel):
    """Planning call 1 contract (parsed, validated; grounding enforced after)."""

    model_config = ConfigDict(extra="ignore")

    segment_applicability: dict[str, str] = Field(default_factory=dict)
    personas: list[PersonaProfile] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    value_proposition: str = ""
    differentiators: list[str] = Field(default_factory=list)
    proof_points: list[ProofPoint] = Field(default_factory=list)
    ctas: dict[str, str] = Field(default_factory=dict)
    messaging_angles: list[MessagingAngle] = Field(default_factory=list)
    channel_emphasis: dict[str, str] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ReuseOutlineItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    asset_id: str
    decision: ReuseDecision
    rationale: str
    reuse_ref: str | None = None
    outline: ContentOutline | None = None


class ReuseOutlinesLLMOutput(BaseModel):
    """Planning call 2 contract."""

    model_config = ConfigDict(extra="ignore")

    items: list[ReuseOutlineItem] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ------------------------------------------------------------------- outcomes


class PlanOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    status: PlanStatus
    pack: AudienceOfferPack | None = None
    checklist: AssetChecklist | None = None
    outlines: list[ContentOutline] = Field(default_factory=list)
    plan: WorkflowPlan | None = None
    workspace_root: str | None = None
    pack_doc_ref: str | None = None
    tracker_ref: str | None = None
    escalation_reasons: list[str] = Field(default_factory=list)


class PackagingOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    status: PlanStatus
    manifest: PackageManifest | None = None
    report: CompletenessReport | None = None
    escalation_reasons: list[str] = Field(default_factory=list)
