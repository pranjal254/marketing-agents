"""Business Capability Layer config — generic shapes (kit build spec §6) + read-only loader.

The shapes are domain-free; domain values live in each agent's versioned JSON config.
Config is read-only at runtime: this module exposes no write or update surface
(kit hard rule 6).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntakeField(BaseModel):
    model_config = ConfigDict(frozen=True)
    field: str
    type: Literal["text", "number", "date", "boolean", "select", "list"]
    required: bool
    options: list[str] | None = None


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    condition: str
    result_action_class: str = Field(alias="resultActionClass")


class ActionClass(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    label: str
    description: str


class AuthorityEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    impact_ceiling: dict[str, str] = Field(alias="impactCeiling")
    reversibility_rules: dict[str, str] = Field(alias="reversibilityRules")
    domain_boundary: str = Field(alias="domainBoundary")
    data_recency_max_days: int = Field(alias="dataRecencyMaxDays")
    compliance_ceiling: str | None = Field(default=None, alias="complianceCeiling")


class RoutingRule(BaseModel):
    model_config = ConfigDict(frozen=True)
    uncertainty_type: Literal["data_ambiguity", "policy_gap", "confidence_only"] = Field(
        alias="uncertaintyType"
    )
    routes_to: str = Field(alias="routesTo")


class TierThresholds(BaseModel):
    model_config = ConfigDict(frozen=True)
    tier1: str
    tier2: str
    tier3: str


class DecisionAgentConfig(BaseModel):
    """Immutable at runtime; validated on load."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_type: Literal["decision"] = Field(alias="agentType")
    agent_id: str = Field(alias="agentId")
    version: str
    intake_schema: list[IntakeField] = Field(alias="intakeSchema")
    policy_rules: list[PolicyRule] = Field(alias="policyRules")
    action_class_taxonomy: list[ActionClass] = Field(alias="actionClassTaxonomy")
    authority_envelope: AuthorityEnvelope = Field(alias="authorityEnvelope")
    routing_map: list[RoutingRule] = Field(alias="routingMap")
    tier_thresholds: TierThresholds = Field(alias="tierThresholds")
    reason_codes: list[str] = Field(alias="reasonCodes")
    precedent_decay_days: int = Field(default=90, alias="precedentDecayDays")
    reasoning_provider: Literal["claude", "local", "rubric"] = Field(
        default="claude", alias="reasoningProvider"
    )

    def route_for(self, uncertainty_type: str) -> str:
        for rule in self.routing_map:
            if rule.uncertainty_type == uncertainty_type:
                return rule.routes_to
        raise KeyError(f"no routing rule for uncertainty type {uncertainty_type!r}")

    def action_class_ids(self) -> list[str]:
        return [a.id for a in self.action_class_taxonomy]


def load_decision_config(path: str | Path) -> DecisionAgentConfig:
    """Load + validate a versioned decision-agent config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return DecisionAgentConfig.model_validate(json.load(f))
