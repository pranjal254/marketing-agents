"""Versioned Business Capability config for the orchestrator — read-only at runtime
(kit hard rule 6: no write or update surface exists)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CompositionItem(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    asset_type: str = Field(alias="assetType")
    label: str
    required: bool
    volume_cap: int = Field(alias="volumeCap", ge=1)
    playbook: str | None = None
    is_researched_blog: bool = Field(alias="isResearchedBlog")
    review_gate: Literal["flagship", "derivative"] = Field(alias="reviewGate")


class Capacity(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    researched_blogs_per_month: int = Field(alias="researchedBlogsPerMonth", ge=1)


class ReviewGates(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    flagship_business_days: int = Field(alias="flagshipBusinessDays", ge=1)
    derivative_business_days: int = Field(alias="derivativeBusinessDays", ge=1)


class DraftingDays(BaseModel):
    model_config = ConfigDict(frozen=True)
    flagship: int = Field(ge=0)
    derivative: int = Field(ge=0)


class Thresholds(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    thin_intel_unverified_share: float = Field(alias="thinIntelUnverifiedShare", ge=0.0, le=1.0)
    reuse_fitness: float = Field(alias="reuseFitness", ge=0.0, le=1.0)
    adapt_fitness: float = Field(alias="adaptFitness", ge=0.0, le=1.0)


class FitnessWeights(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    asset_type: float = Field(alias="assetType")
    vertical: float
    business_unit: float = Field(alias="businessUnit")
    topic: float


class Naming(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    campaign_folder: str = Field(alias="campaignFolder")
    asset_file: str = Field(alias="assetFile")


class RoutingEntry(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    reason: str
    routes_to: str = Field(alias="routesTo")


class OrchestratorConfig(BaseModel):
    """Immutable at runtime; validated on load."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_type: Literal["orchestrator"] = Field(alias="agentType")
    agent_id: str = Field(alias="agentId")
    version: str
    composition_status: str = Field(alias="compositionStatus")
    composition: list[CompositionItem]
    capacity: Capacity
    review_gates: ReviewGates = Field(alias="reviewGates")
    drafting_business_days: DraftingDays = Field(alias="draftingBusinessDays")
    thresholds: Thresholds
    fitness_weights: FitnessWeights = Field(alias="fitnessWeights")
    confirmation_sla_business_days: int = Field(alias="confirmationSlaBusinessDays", ge=1)
    naming: Naming
    workspace_folders: list[str] = Field(alias="workspaceFolders")
    routing_map: list[RoutingEntry] = Field(alias="routingMap")
    reason_codes: list[str] = Field(alias="reasonCodes")
    brand_rules_version: str = Field(alias="brandRulesVersion")

    def route_for(self, reason: str) -> str:
        for entry in self.routing_map:
            if entry.reason == reason:
                return entry.routes_to
        raise KeyError(f"no routing rule for reason {reason!r}")

    def required_items(self) -> list[CompositionItem]:
        return [c for c in self.composition if c.required]

    def item_for(self, asset_type: str) -> CompositionItem:
        for c in self.composition:
            if c.asset_type == asset_type:
                return c
        raise KeyError(f"asset type {asset_type!r} not in composition")


def load_orchestrator_config(path: str | Path) -> OrchestratorConfig:
    """Load + validate the versioned config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return OrchestratorConfig.model_validate(json.load(f))
