"""Step 1: load the approved campaign brief + intake context from the Context Store.

A brief that is not in ``approved`` state is rejected with a structured error —
the orchestrator never plans from an unapproved brief.
"""

from __future__ import annotations

from typing import Any

from shiftai_shared.context_store.store import ContextStore

# Written by Agent 1 (campaign_identification.persistence) — shared store contract.
KIND_APPROVED_BRIEF = "approved_brief"
KIND_INTAKE_CONTEXT = "intake_context"


class BriefNotApprovedError(Exception):
    """Structured rejection: the referenced brief is missing or not approved."""

    def __init__(self, campaign_id: str, detail: str) -> None:
        super().__init__(f"brief {campaign_id!r} rejected: {detail}")
        self.campaign_id = campaign_id
        self.detail = detail


class ApprovedBrief:
    """Read-only view over the approved brief record Agent 1 released."""

    def __init__(self, campaign_id: str, record: dict[str, Any]) -> None:
        brief = record.get("brief") or {}
        self.campaign_id = campaign_id
        self.case_id = str(brief.get("case_id", ""))
        self.doc_ref = str(record.get("doc_ref", ""))
        self.status = str(brief.get("status", ""))
        self.fields: dict[str, str] = {
            str(f.get("name")): str(f.get("value", ""))
            for f in brief.get("fields", [])
            if f.get("name")
        }
        self.provenance: dict[str, str] = {
            str(f.get("name")): str(f.get("provenance", ""))
            for f in brief.get("fields", [])
            if f.get("name")
        }
        self.classification: dict[str, Any] = dict(brief.get("classification") or {})
        self.raw = record

    def field(self, name: str, default: str = "") -> str:
        return self.fields.get(name, default)

    @property
    def topic(self) -> str:
        return self.field("offer_topic") or self.field("objective")

    @property
    def window(self) -> tuple[str, str]:
        return self.field("timeline_start"), self.field("timeline_end")


def load_approved_brief(store: ContextStore, campaign_id: str) -> ApprovedBrief:
    record = store.get(KIND_APPROVED_BRIEF, campaign_id)
    if record is None:
        raise BriefNotApprovedError(campaign_id, "no approved brief record exists")
    brief = ApprovedBrief(campaign_id, record.value)
    if brief.status != "approved":
        raise BriefNotApprovedError(campaign_id, f"brief status is {brief.status!r}")
    start, end = brief.window
    if not start or not end:
        raise BriefNotApprovedError(campaign_id, "approved brief has no campaign window")
    return brief
