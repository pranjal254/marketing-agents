"""Process-context mechanics for STS v2 — the engine half, domain-free.

STS defines ``shiftai.stage.id`` but leaves its vocabulary to the business
capability that owns the process. This module therefore knows only the shape of
a process context, never the steps of any particular one: the stage vocabulary
is versioned capability content and is injected (see ``shiftai_shared.process``).

An emitter carries a ProcessContext so every record it writes says which process
and which step of it the record belongs to, which is what a dashboard needs to
report by business step instead of by agent internals.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

PROCESS_NAME_ATTRIBUTE = "shiftai.process.name"
PROCESS_VERSION_ATTRIBUTE = "shiftai.process.version"
STAGE_ATTRIBUTE = "shiftai.stage.id"
STAGE_ORDINAL_ATTRIBUTE = "shiftai.stage.ordinal"


@dataclass(frozen=True)
class ProcessContext:
    """What an agent stamps on every record about where it sits in a process.

    ``default_stage_id`` is the step the agent occupies most of the time; a call
    that belongs to a different step passes ``shiftai.stage.id`` explicitly and
    overrides it. ``stage_ordinals`` is the injected ordering table used to
    derive ``shiftai.stage.ordinal`` for whichever stage ends up on the record,
    so consumers can sort steps without carrying the vocabulary themselves.
    """

    name: str
    version: str
    default_stage_id: str | None = None
    stage_ordinals: Mapping[str, int] = field(default_factory=dict)

    def static_attributes(self) -> dict[str, str]:
        attrs = {
            PROCESS_NAME_ATTRIBUTE: self.name,
            PROCESS_VERSION_ATTRIBUTE: self.version,
        }
        if self.default_stage_id:
            attrs[STAGE_ATTRIBUTE] = self.default_stage_id
        return attrs

    def ordinal_for(self, stage_id: str | None) -> int | None:
        if not stage_id:
            return None
        return self.stage_ordinals.get(stage_id)
