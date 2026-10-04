"""Content to Campaign process pack — versioned, read-only Business Capability
content, the same category as the brand rules pack.

The nine-step journey is a business definition, not engine mechanics: it is what
the studio shows people and what an execution-studio dashboard groups by. The
engine half (the ProcessContext shape, the STS attribute names) lives in
``shiftai_shared.telemetry.process`` and knows nothing about these steps.

Agents call ``process_context("<agent_id>")`` once, at emitter construction, and
every record they emit then carries the process name, the pack version, the
stage the agent sits in, and that stage's ordinal. A call belonging to a
different step overrides ``shiftai.stage.id`` at the emit site.
"""

from __future__ import annotations

import json
from importlib import resources

from pydantic import BaseModel, ConfigDict, Field

from shiftai_shared.telemetry.process import ProcessContext

JOURNEY_VERSION = "1.0.0"
_JOURNEY_FILE = "journey_content_to_campaign_v1_0_0.json"


class Stage(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    stage_id: str = Field(alias="stageId")
    ordinal: int = Field(ge=1)
    label: str
    description: str


class ProcessPack(BaseModel):
    """Immutable at runtime; validated on load. No write surface exists."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    process_name: str = Field(alias="processName")
    version: str
    label: str
    description: str
    stages: list[Stage]
    agent_default_stages: dict[str, str] = Field(alias="agentDefaultStages")

    def stage(self, stage_id: str) -> Stage | None:
        return next((s for s in self.stages if s.stage_id == stage_id), None)

    def ordinals(self) -> dict[str, int]:
        return {s.stage_id: s.ordinal for s in self.stages}


def _load() -> ProcessPack:
    raw = resources.files(__package__).joinpath(_JOURNEY_FILE).read_text(encoding="utf-8")
    pack = ProcessPack.model_validate(json.loads(raw))
    known = {s.stage_id for s in pack.stages}
    unknown = sorted(set(pack.agent_default_stages.values()) - known)
    if unknown:
        raise ValueError(f"process pack maps agents to unknown stages: {unknown}")
    return pack


PROCESS_PACK: ProcessPack = _load()
PROCESS_NAME: str = PROCESS_PACK.process_name

STAGE_INTAKE = "intake"
STAGE_AUDIENCE_OFFER = "audience_offer"
STAGE_ASSET_PLAN = "asset_plan"
STAGE_DRAFTING = "drafting"
STAGE_REVIEW = "review"
STAGE_PACKAGING = "packaging"
STAGE_COMPLIANCE = "compliance"
STAGE_GRAMMAR_QA = "grammar_qa"
STAGE_SIGNOFF = "signoff"


def process_context(agent_id: str) -> ProcessContext:
    """The ProcessContext an agent hands its emitter.

    An agent absent from the pack still gets process identity; it just carries no
    default stage, so its records stay unclassified rather than mislabelled.
    """
    return ProcessContext(
        name=PROCESS_PACK.process_name,
        version=PROCESS_PACK.version,
        default_stage_id=PROCESS_PACK.agent_default_stages.get(agent_id),
        stage_ordinals=PROCESS_PACK.ordinals(),
    )


def stages() -> list[Stage]:
    return list(PROCESS_PACK.stages)
