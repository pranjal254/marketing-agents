"""Versioned Business Capability config for the Collaboration & Iteration Agent —
read-only at runtime (kit hard rule 6: no write or update surface exists)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ReviewGate = Literal["flagship", "derivative"]
ReviewerFocus = Literal["editorial", "message_fit"]


class ReviewerSlot(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str
    focus: ReviewerFocus


class ReminderLadder(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    first_reminder_bd: int = Field(alias="firstReminderBusinessDaysAfterDue", ge=0)
    second_reminder_bd: int = Field(alias="secondReminderBusinessDaysAfterDue", ge=0)
    escalate_bd: int = Field(alias="escalateBusinessDaysAfterDue", ge=0)


class RoutingEntry(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    reason: str
    routes_to: str = Field(alias="routesTo")


class CollaborationConfig(BaseModel):
    """Immutable at runtime; validated on load."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    agent_type: Literal["decision"] = Field(alias="agentType")
    agent_id: str = Field(alias="agentId")
    version: str
    reviewer_map_status: str = Field(alias="reviewerMapStatus")
    flagship_asset_type: str = Field(alias="flagshipAssetType")
    reviewer_map: dict[ReviewGate, list[ReviewerSlot]] = Field(alias="reviewerMap")
    reminder_ladder: ReminderLadder = Field(alias="reminderLadder")
    max_rounds_alert: int = Field(alias="maxRoundsAlert", ge=1)
    routing_map: list[RoutingEntry] = Field(alias="routingMap")
    reason_codes: list[str] = Field(alias="reasonCodes")
    brand_rules_version: str = Field(alias="brandRulesVersion")

    def route_for(self, reason: str) -> str:
        for entry in self.routing_map:
            if entry.reason == reason:
                return entry.routes_to
        raise KeyError(f"no routing rule for reason {reason!r}")

    def reviewers_for(self, gate: ReviewGate) -> list[ReviewerSlot]:
        return list(self.reviewer_map.get(gate, []))


def load_collaboration_config(path: str | Path) -> CollaborationConfig:
    """Load + validate the versioned config. Read-only — no save exists."""
    with open(path, encoding="utf-8") as f:
        return CollaborationConfig.model_validate(json.load(f))
