"""Typed I/O models for the Content Repurposing Agent (spec Inputs/Outputs)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RepurposeStatus = Literal[
    "flagship_staged",  # flagship drafted + staged for the human review cycle (step 4)
    "flagship_confirmed",  # human content-confirmed record exists — fan-out may run
    "derivatives_staged",  # fan-out complete; drafts staged (withheld subset flagged)
    "escalated",
    "failed",
]

DraftState = Literal["staged", "withheld"]


# ------------------------------------------------------------- claim provenance


class ClaimMarker(BaseModel):
    """One inline ``[c-N]`` marker in a draft: claim text → verified source ref.
    Spec step 3: inline source markers so reviewers verify provenance without
    re-research (dev binding: marker table in the .docx + sidecar claim map;
    real Word comments arrive with the Graph/OneDrive binding)."""

    model_config = ConfigDict(extra="ignore")

    marker: str
    claim: str
    source_ref: str


class GapNote(BaseModel):
    """A section or claim NOT drafted because it cannot be sourced —
    explicit gap, never plausible prose (spec guardrail 1)."""

    gap_id: str
    asset_id: str
    section: str
    needed: str
    created_at: str = ""


class SelfCheckReport(BaseModel):
    """Generation-time self-check vs the rules pack (spec step 8): deterministic
    brand lint + unsourced-numeric detection. The Quality Gate stays the
    authority; this keeps its failure rate low."""

    passed: bool
    attempts: int = 1
    findings: list[dict[str, str]] = Field(default_factory=list)
    unsourced_numeric_tokens: list[str] = Field(default_factory=list)
    missing_brand_mention: bool = False


class DraftSection(BaseModel):
    heading: str
    paragraphs: list[str] = Field(default_factory=list)


class StagedDraft(BaseModel):
    """A versioned draft staged in the campaign workspace and registered in the
    Context Store with claim lineage (spec step 9). Additive — a regeneration is
    a new version, never an overwrite of reviewed content."""

    campaign_id: str
    asset_id: str
    asset_type: str
    kind: Literal["flagship", "derivative"]
    title: str
    version: int
    filename: str
    file_ref: str
    claim_map_ref: str
    sections: list[DraftSection] = Field(default_factory=list)
    claim_markers: list[ClaimMarker] = Field(default_factory=list)
    claim_lineage: list[str] = Field(default_factory=list)  # inventory claim_ids (derivatives)
    self_check: SelfCheckReport
    gap_notes: list[GapNote] = Field(default_factory=list)
    status: DraftState = "staged"
    outline_version: int = 0
    inventory_version: int = 0
    rework_of_version: int | None = None
    created_at: str = ""


# ------------------------------------------------------------- claim inventory


class ClaimInventoryItem(BaseModel):
    """One reusable claim/quote/data point extracted from the CONFIRMED flagship.
    ``quote`` must be a verbatim flagship substring — verified in code, and the
    single source all derivatives draw from (spec step 5)."""

    model_config = ConfigDict(extra="ignore")

    claim_id: str
    kind: Literal["claim", "quote", "data_point", "structure"]
    text: str
    quote: str
    source_ref: str


class ClaimInventory(BaseModel):
    campaign_id: str
    flagship_version: int
    items: list[ClaimInventoryItem] = Field(default_factory=list)
    method: Literal["llm_verified", "deterministic_fallback"]
    dropped_unverified: int = 0
    created_at: str = ""


# ------------------------------------------------------------- LLM contracts


class FlagshipLLMSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    heading: str
    paragraphs: list[str] = Field(default_factory=list)


class FlagshipGapNoteOut(BaseModel):
    model_config = ConfigDict(extra="ignore")

    section: str
    needed: str


class FlagshipLLMOutput(BaseModel):
    """Flagship drafting contract (parsed, validated; grounding enforced after)."""

    model_config = ConfigDict(extra="ignore")

    title: str = ""
    sections: list[FlagshipLLMSection] = Field(default_factory=list)
    claims_used: list[ClaimMarker] = Field(default_factory=list)
    gap_notes: list[FlagshipGapNoteOut] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class InventoryLLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[ClaimInventoryItem] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class DerivativeVariant(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str = ""
    paragraphs: list[str] = Field(default_factory=list)


class DerivativeLLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    variants: list[DerivativeVariant] = Field(default_factory=list)
    claims_used: list[str] = Field(default_factory=list)  # inventory claim_ids
    gap_notes: list[FlagshipGapNoteOut] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# ------------------------------------------------------------------- outcomes


class FlagshipOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    status: RepurposeStatus
    draft: StagedDraft | None = None
    gap_notes: list[GapNote] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)


class FanoutOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    status: RepurposeStatus
    staged: list[StagedDraft] = Field(default_factory=list)
    withheld: list[str] = Field(default_factory=list)  # asset_ids that failed self-check
    skipped: list[str] = Field(default_factory=list)  # reuse assets / no recipe
    inventory: ClaimInventory | None = None
    gap_notes: list[GapNote] = Field(default_factory=list)
    escalation_reasons: list[str] = Field(default_factory=list)


class ReworkOutcome(BaseModel):
    case_id: str
    trace_id: str
    campaign_id: str
    status: RepurposeStatus
    draft: StagedDraft | None = None
    escalation_reasons: list[str] = Field(default_factory=list)
