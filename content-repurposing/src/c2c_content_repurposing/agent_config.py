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

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ChannelRecipe(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    asset_type: str = Field(alias="assetType")
    label: str
    recipe: str
    must_name_brand: bool = Field(alias="mustNameBrand")


class WordRange(BaseModel):
    """Target length for one variant of an asset, in words."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    min_words: int = Field(alias="min", ge=1)
    max_words: int = Field(alias="max", ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> WordRange:
        if self.max_words < self.min_words:
            raise ValueError(
                f"word range max ({self.max_words}) is below min ({self.min_words})"
            )
        return self


class ContentLimits(BaseModel):
    """Per-campaign content settings are bounded by config, not by the caller.

    A Content Writer tunes how many variants of each asset to produce and how
    long each should be, but only inside these limits: the hard variant ceiling
    is a governance decision (over-production was rejected in the TO-BE design
    review), and the word floor and ceiling keep a typo from asking the model
    for a one-word or a novel-length asset. Per-asset-type ranges here are the
    defaults a campaign starts from.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_variants_per_asset: int = Field(alias="maxVariantsPerAsset", default=5, ge=1)
    word_floor: int = Field(alias="wordFloor", default=40, ge=1)
    word_ceiling: int = Field(alias="wordCeiling", default=5_000, ge=1)
    default_word_range: WordRange = Field(
        alias="defaultWordRange", default=WordRange(min=120, max=300)
    )
    by_asset_type: dict[str, WordRange] = Field(alias="byAssetType", default_factory=dict)

    @model_validator(mode="after")
    def _ranges_within_bounds(self) -> ContentLimits:
        if self.word_ceiling < self.word_floor:
            raise ValueError("wordCeiling is below wordFloor")
        for asset_type, rng in {**self.by_asset_type, "default": self.default_word_range}.items():
            if rng.min_words < self.word_floor or rng.max_words > self.word_ceiling:
                raise ValueError(
                    f"word range for {asset_type!r} falls outside "
                    f"[{self.word_floor}, {self.word_ceiling}]"
                )
        return self

    def word_range_for(self, asset_type: str) -> WordRange:
        return self.by_asset_type.get(asset_type, self.default_word_range)


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
    content_limits: ContentLimits = Field(
        alias="contentLimits", default_factory=ContentLimits
    )
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

    def word_range_for(self, asset_type: str) -> WordRange:
        return self.content_limits.word_range_for(asset_type)


def load_repurposing_config(path: str | Path) -> RepurposingConfig:
    """Load + validate the versioned config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return RepurposingConfig.model_validate(json.load(f))
