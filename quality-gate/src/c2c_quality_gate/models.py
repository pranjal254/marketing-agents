"""Typed domain objects for the Quality Gate & Approval Agent.

Verdicts are a pure function of blocking-finding count (spec Explainability);
severity policy lives in code keyed by rule ID — blocking is reserved for
documented governance rules, style preferences are advisory (guardrail 1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["blocking", "advisory"]
PassSource = Literal["deterministic", "contextual"]
Verdict = Literal["pass", "fail"]
TaskScope = Literal["asset", "package"]
TaskStatus = Literal["open", "approved", "returned", "cancelled"]
PackageStatus = Literal["gate_failed", "in_review", "returned", "approved_locked", "invalidated"]
CalibrationKind = Literal["false_positive", "false_negative", "dispute"]

# The contextual pass evaluates EXACTLY these rules (spec system prompt); the
# gate assigns severity by policy — the model's own severity is never trusted.
CONTEXTUAL_RULES: dict[str, Severity] = {
    "bc_fo_meaning": "blocking",
    "copilot_scope": "blocking",
    "claim_sourcing": "blocking",
    "tone_urgency_fear": "blocking",
    "brand_voice": "advisory",
}


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    severity: Severity
    source: PassSource
    location: str = ""
    quote: str = ""
    quote_verified: bool = True  # quote found verbatim in the asset text
    reasoning: str = ""
    remediation: str = ""  # direction only — the gate never writes replacement text


class AssetReport(BaseModel):
    campaign_id: str
    asset_id: str
    asset_type: str
    version: int
    sha256: str
    findings: list[Finding] = Field(default_factory=list)
    verdict: Verdict
    checks_complete: bool = True
    incomplete_reason: str = ""
    deterministic_ms: int = 0
    contextual_ms: int = 0
    rules_pack_id: str = ""
    rules_pack_version: str = ""
    config_version: str = ""
    checked_at: str = ""

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "blocking"]


class ReviewTask(BaseModel):
    task_id: str
    campaign_id: str
    scope: TaskScope
    asset_id: str = ""  # empty for package scope
    step: str
    role: str
    sequence_index: int
    sla_business_days: int
    due: str
    status: TaskStatus = "open"
    reminders_sent: int = 0
    escalated: bool = False
    created_at: str = ""
    decided_by: str | None = None
    decided_role: str | None = None
    decided_at: str | None = None
    notes: str = ""


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    approval_id: str
    campaign_id: str
    scope: TaskScope
    asset_id: str = ""
    step: str
    decision: Literal["approved", "returned"]
    actor_id: str
    actor_role: str
    asset_version: int = 0
    sha256: str = ""
    notes: str = ""
    at: str = ""


class LockRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    asset_id: str
    final_ref: str
    sha256: str


class PackageGateState(BaseModel):
    campaign_id: str
    manifest_id: str
    manifest_version: int
    status: PackageStatus
    asset_verdicts: dict[str, Verdict] = Field(default_factory=dict)
    failed_asset_ids: list[str] = Field(default_factory=list)
    locks: list[LockRecord] = Field(default_factory=list)
    approved_at: str | None = None
    gate_started_at: str = ""
    slip_escalated: bool = False
    updated_at: str = ""


class CalibrationEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    campaign_id: str
    asset_id: str
    rule_id: str
    kind: CalibrationKind
    reviewer_id: str
    reviewer_role: str
    notes: str = ""
    at: str = ""


class GateOutcome(BaseModel):
    campaign_id: str
    manifest_version: int
    status: PackageStatus
    reports: list[AssetReport] = Field(default_factory=list)
    failed_asset_ids: list[str] = Field(default_factory=list)
    tasks_created: int = 0
    reused_reports: int = 0


class SweepOutcome(BaseModel):
    reminders_sent: int = 0
    reviews_escalated: int = 0
    package_slips_escalated: int = 0
    details: list[str] = Field(default_factory=list)


# ------------------------------------------------------------- LLM contract


class ContextualFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rule_id: str
    severity: str = ""
    location: str = ""
    quote: str = ""
    reasoning: str = ""
    remediation: str = ""


class ContextualLLMOutput(BaseModel):
    """Contextual-pass contract. ``checks_completed`` must name every contextual
    rule — a missing rule means the check did not complete (fail-closed, spec
    step 5: never default a rule to pass)."""

    model_config = ConfigDict(extra="ignore")

    findings: list[ContextualFinding] = Field(default_factory=list)
    checks_completed: list[str] = Field(default_factory=list)
    confidence: float = 0.0
