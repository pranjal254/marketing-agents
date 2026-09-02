"""Task 7 — assemble the structured campaign brief (deterministic packaging of the
validated request + classification; the Word document is built with no LLM).

Every populated field carries its provenance (guardrail 1). Template version
0.1.0-draft pending Marketing Lead review of the standard template (PLAN.md Q7).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from shiftai_shared.m365.word import DocSection, DocSpec, build_docx

from campaign_identification import BRIEF_TEMPLATE_VERSION
from campaign_identification.models import (
    BcFoCheck,
    BriefField,
    CampaignBrief,
    CampaignRequest,
    Classification,
    ConflictFlag,
)

_SOURCE_LABEL = {
    "form": "intake form",
    "plan": "quarterly marketing plan",
    "calendar": "event calendar",
    "adhoc": "ad-hoc request",
}

_BRIEF_FIELD_ORDER = [
    "objective",
    "business_unit",
    "vertical",
    "target_segment",
    "offer_topic",
    "channels",
    "timeline_start",
    "timeline_end",
    "owner",
    "budget_flag",
    "requester",
]


def _provenance(request: CampaignRequest, field: str, normalized: dict[str, str]) -> str:
    base = _SOURCE_LABEL[request.source]
    refs = f" ({'; '.join(request.source_refs)})" if request.source_refs else ""
    note = " · normalized by agent" if field in normalized else ""
    return f"{base}{refs}{note}"


def assemble_brief(
    *,
    case_id: str,
    request: CampaignRequest,
    classification: Classification | None,
    conflicts: list[ConflictFlag],
    bc_fo: BcFoCheck,
    normalized_fields: dict[str, str],
    version: int,
    campaign_id: str | None = None,
) -> CampaignBrief:
    fields: list[BriefField] = []
    for name in _BRIEF_FIELD_ORDER:
        raw_value = normalized_fields.get(name, getattr(request, name, None))
        if raw_value in (None, "", []):
            continue
        value = ", ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
        fields.append(
            BriefField(
                name=name, value=value, provenance=_provenance(request, name, normalized_fields)
            )
        )
    return CampaignBrief(
        campaign_id=campaign_id or f"cmp_{uuid.uuid4().hex[:10]}",
        case_id=case_id,
        version=version,
        status="awaiting_approval",
        fields=fields,
        classification=classification,
        conflicts=conflicts,
        bc_fo=bc_fo if bc_fo.mixed else None,
        template_version=BRIEF_TEMPLATE_VERSION,
        created_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
    )


def brief_docx(brief: CampaignBrief) -> bytes:
    """Materialize the brief as a Word document per the standard template."""
    sections: list[DocSection] = [
        DocSection(
            heading="Brief Fields",
            table_header=("Field", "Value"),
            table_rows=tuple((f.name, f.value) for f in brief.fields),
        ),
        DocSection(
            heading="Provenance",
            paragraphs=("Every populated field carries its source (never inferred).",),
            table_header=("Field", "Source"),
            table_rows=tuple((f.name, f.provenance) for f in brief.fields),
        ),
    ]
    if brief.classification is not None:
        c = brief.classification
        rationale = tuple((field, source) for field, source in sorted(c.field_rationale.items()))
        sections.append(
            DocSection(
                heading="Classification",
                table_header=("Attribute", "Value"),
                table_rows=(
                    ("campaign_type", c.campaign_type),
                    ("priority", c.priority),
                    ("channel_mix", ", ".join(c.channel_mix)),
                    ("segment_relevance", c.segment_relevance),
                    *rationale,
                ),
            )
        )
    if brief.conflicts:
        sections.append(
            DocSection(
                heading="Duplicate / Conflict Flags (human decision required)",
                table_header=("Conflicting campaign", "Rationale"),
                table_rows=tuple(
                    (f"{c.conflicting_campaign_id} [{c.kind}, {c.freshness}]", c.rationale)
                    for c in brief.conflicts
                ),
            )
        )
    if brief.bc_fo is not None and brief.bc_fo.mixed:
        sections.append(
            DocSection(
                heading="BC/F&O Independence — split proposal",
                paragraphs=(
                    "This request mixes Business Central and F&O; concepts are proposed "
                    "separately and must not be merged.",
                    *brief.bc_fo.split_proposal,
                ),
            )
        )
    sections.append(
        DocSection(
            heading="Approval",
            paragraphs=(
                "Status: awaiting BU Campaign Lead approval. "
                "This brief advances only on an explicit recorded human approval.",
            ),
        )
    )
    spec = DocSpec(
        title=f"Campaign Brief — {brief.campaign_id}",
        subtitle=(
            f"Case {brief.case_id} · brief v{brief.version} · "
            f"template {brief.template_version} · created {brief.created_at}"
        ),
        sections=tuple(sections),
    )
    return build_docx(spec)


def brief_filename(brief: CampaignBrief) -> str:
    return f"{brief.campaign_id}-brief-v{brief.version}.docx"
