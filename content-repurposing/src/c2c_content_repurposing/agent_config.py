"""Versioned Business Capability config for the Content Repurposing Agent —
read-only at runtime (kit hard rule 6: no write or update surface exists).

Channel recipes implement the spec's source-to-derivative map as config, not
judgment (spec guardrail 4: fan-out tuning is config — over-production was
explicitly rejected in the TO-BE design review). Volume caps come from the
approved asset checklist (Agent 2's composition is the authority on volumes).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChannelRecipe(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    asset_type: str = Field(alias="assetType")
    label: str
    recipe: str
    must_name_brand: bool = Field(alias="mustNameBrand")


class RoutingEntry(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    reason: str
    routes_to: str = Field(alias="routesTo")


class RepurposingConfig(BaseModel):
    """Immutable at runtime; validated on load."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_type: Literal["decision"] = Field(alias="agentType")
    agent_id: str = Field(alias="agentId")
    version: str
    recipe_status: str = Field(alias="recipeStatus")
    flagship_asset_type: str = Field(alias="flagshipAssetType")
    max_regenerations: int = Field(alias="maxRegenerations", ge=0)
    truncation_raise_factor: float = Field(alias="truncationRaiseFactor", ge=1.0)
    recipes: list[ChannelRecipe]
    routing_map: list[RoutingEntry] = Field(alias="routingMap")
    reason_codes: list[str] = Field(alias="reasonCodes")
    brand_rules_version: str = Field(alias="brandRulesVersion")

    def route_for(self, reason: str) -> str:
        for entry in self.routing_map:
            if entry.reason == reason:
                return entry.routes_to
        raise KeyError(f"no routing rule for reason {reason!r}")

    def recipe_for(self, asset_type: str) -> ChannelRecipe | None:
        for recipe in self.recipes:
            if recipe.asset_type == asset_type:
                return recipe
        return None


def load_repurposing_config(path: str | Path) -> RepurposingConfig:
    """Load + validate the versioned config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return RepurposingConfig.model_validate(json.load(f))
