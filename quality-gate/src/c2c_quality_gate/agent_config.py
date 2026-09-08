"""Versioned Business Capability config for the Quality Gate & Approval Agent —
read-only at runtime (kit hard rule 6: no write or update surface exists).

Guardrail 4: ALL rules live here and in the brand rules pack, both versioned and
Marketing-owned; the agent adds no rules of its own, and every verdict and route
cites the config version that produced it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DistributionClass = Literal["public", "internal"]


class ReviewStep(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    step: str
    role: str
    sla_business_days: int = Field(alias="slaBusinessDays", ge=1)


class RoutingEntry(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    reason: str
    routes_to: str = Field(alias="routesTo")


class QualityGateConfig(BaseModel):
    """Immutable at runtime; validated on load."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_type: Literal["hybrid"] = Field(alias="agentType")
    agent_id: str = Field(alias="agentId")
    version: str
    policy_status: str = Field(alias="policyStatus")
    distribution_classes: dict[str, DistributionClass] = Field(alias="distributionClasses")
    review_sequences: dict[DistributionClass, list[ReviewStep]] = Field(alias="reviewSequences")
    package_signoff: ReviewStep = Field(alias="packageSignoff")
    package_sla_business_days: int = Field(alias="packageSlaBusinessDays", ge=1)
    reminder_thresholds: list[float] = Field(alias="reminderThresholds")
    aeo_named_mention_asset_types: list[str] = Field(alias="aeoNamedMentionAssetTypes")
    strategy_visibility_role: str = Field(alias="strategyVisibilityRole")
    routing_map: list[RoutingEntry] = Field(alias="routingMap")
    reason_codes: list[str] = Field(alias="reasonCodes")
    brand_rules_version: str = Field(alias="brandRulesVersion")

    def route_for(self, reason: str) -> str:
        for entry in self.routing_map:
            if entry.reason == reason:
                return entry.routes_to
        raise KeyError(f"no routing rule for reason {reason!r}")

    def class_for(self, asset_type: str) -> DistributionClass:
        """Policy lookup, never inference (spec step 7). Unknown asset types are
        treated as PUBLIC — the deeper review path (fail-closed direction)."""
        return self.distribution_classes.get(asset_type, "public")

    def sequence_for(self, distribution_class: DistributionClass) -> list[ReviewStep]:
        return list(self.review_sequences.get(distribution_class, []))


def load_quality_gate_config(path: str | Path) -> QualityGateConfig:
    """Load + validate the versioned config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return QualityGateConfig.model_validate(json.load(f))
