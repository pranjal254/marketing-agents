"""Deterministic document rendering — no LLM in this module.

The audience & offer pack is materialized as Word + JSON (spec Outputs); the
workflow plan becomes the status tracker (CSV bytes uploaded through the additive
Workspace protocol; the production binding moves to the Excel tracker workbook at
Execution Studio onboarding — recorded in agent-spec.md).
"""

from __future__ import annotations

import csv
import io
import json

from shiftai_shared.m365.word import DocSection, DocSpec, build_docx

from c2c_campaign_box.models import AssetChecklist, AudienceOfferPack, WorkflowPlan


def brief_filename(campaign_id: str, version: int) -> str:
    return f"{campaign_id}-approved-brief-v{version}.docx"


def brief_docx(
    campaign_id: str,
    fields: dict[str, str],
    provenance: dict[str, str],
    doc_ref: str,
) -> bytes:
    """Snapshot of the approved brief for the campaign's brief/ folder — humans
    working in the workspace see the source of record without leaving it. The
    Context Store record stays authoritative; this is a rendered copy."""
    rows = tuple(
        (
            name,
            f"{value} (provenance: {provenance[name]})" if provenance.get(name) else value,
        )
        for name, value in sorted(fields.items())
    )
    return build_docx(
        DocSpec(
            title=f"Approved campaign brief: {campaign_id}",
            subtitle=(
                f"Released by the Campaign Identification Agent (source: {doc_ref})"
                if doc_ref else "Released by the Campaign Identification Agent"
            ),
            sections=(
                DocSection(
                    heading="Brief fields",
                    table_header=("Field", "Value"),
                    table_rows=rows,
                ),
            ),
        )
    )


def pack_filename(pack: AudienceOfferPack) -> str:
    return f"{pack.campaign_id}-audience-offer-pack-v{pack.version}.docx"


def pack_json_filename(pack: AudienceOfferPack) -> str:
    return f"{pack.campaign_id}-audience-offer-pack-v{pack.version}.json"


def pack_docx(pack: AudienceOfferPack, checklist: AssetChecklist | None = None) -> bytes:
    sections: list[DocSection] = [
        DocSection(
            number="01", heading="Audience",
            intro="Who this campaign is for, grounded in the approved brief and sourced intel.",
            paragraphs=(
                f"Priority vertical: {pack.vertical}",
                *(f"Segment {seg}: {rat}" for seg, rat in pack.segment_applicability.items()),
                *(f"Exclusion: {e}" for e in pack.exclusions),
            ),
        ),
        DocSection(
            number="02", heading="Buyer personas",
            intro="The people who decide, and the pains that make this campaign relevant.",
            columns=("Persona", "Role pains", "Grounding"),
            grid=tuple((p.title, p.role_pains, p.rationale) for p in pack.personas),
        ),
        DocSection(
            number="03", heading="Offer and proposition",
            intro="The value proposition and what sets the offer apart.",
            paragraphs=(
                f"Value proposition: {pack.value_proposition}",
                *(f"Differentiator: {d}" for d in pack.differentiators),
            ),
            table_header=("Funnel stage", "Call to action"),
            table_rows=tuple(pack.ctas.items()),
        ),
        DocSection(
            number="04", heading="Proof points (per-claim provenance)",
            intro="Every claim carries its source. Unverified claims are labeled, never dropped.",
            columns=("Claim", "Source", "Status"),
            grid=tuple((p.claim, p.source_ref, p.status) for p in pack.proof_points),
        ),
        DocSection(
            number="05", heading="Messaging angles",
            intro="Persona-specific angles for the assets, each grounded in brief or intel.",
            columns=("Persona", "Angle", "Grounding"),
            grid=tuple((a.persona_id, a.angle, a.grounding) for a in pack.messaging_angles),
        ),
        DocSection(
            number="06", heading="Channel emphasis",
            intro="Where to invest and why, from the classified channel mix.",
            table_header=("Channel", "Rationale"),
            table_rows=tuple(pack.channel_emphasis.items()),
        ),
    ]
    if pack.gaps:
        sections.append(
            DocSection(
                heading="Open gaps (explicit, never filled with plausible content)",
                intro="What the agent could not source. Resolve before production, never invent.",
                paragraphs=tuple(pack.gaps),
            )
        )
    if checklist is not None:
        sections.append(
            DocSection(
                number="07", heading="Asset checklist (reuse / adapt / create)",
                intro="The proposed asset set and how each will be produced.",
                columns=("Asset", "Decision", "Rationale"),
                grid=tuple(
                    (
                        f"{i.label} ({i.asset_type})",
                        i.decision + (" [reuse-check pending]" if i.reuse_check_pending else ""),
                        i.decision_rationale,
                    )
                    for i in checklist.items
                ),
            )
        )
    sections.append(
        DocSection(
            heading="Confirmation gate",
            intro=(
                "This pack is a PROPOSAL. It takes effect only after the BU Campaign "
                "Lead records an explicit confirmation; the orchestrator never confirms "
                "its own output."
            ),
            table_rows=(
                ("Intel mode", pack.intel_mode),
                ("Unverified proof-point share", f"{round(pack.unverified_share * 100)}%"),
                ("Status", "Awaiting confirmation"),
            ),
        )
    )
    subtitle = (
        f"Campaign {pack.campaign_id}  |  Pack v{pack.version}  |  "
        f"Intel mode: {pack.intel_mode}  |  Template {pack.template_version}"
    )
    meta = (
        ("Campaign ID", pack.campaign_id),
        ("Pack version", f"v{pack.version}"),
        ("Priority vertical", pack.vertical),
        ("Prepared by", "Campaign-in-a-Box Orchestrator (proposal, awaiting confirmation)"),
        ("Created", pack.created_at or "on generation"),
    )
    return _pack_spec(
        subtitle=subtitle, meta=meta, sections=tuple(sections)
    )


def _pack_spec(*, subtitle: str, meta: tuple[tuple[str, str], ...],
               sections: tuple[DocSection, ...]) -> bytes:
    return build_docx(
        DocSpec(
            kicker="Audience and offer pack",
            title="Audience & Offer Pack",
            subtitle=subtitle,
            meta=meta,
            sections=sections,
        )
    )


def pack_json(pack: AudienceOfferPack) -> bytes:
    return json.dumps(pack.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")


def tracker_filename(plan: WorkflowPlan) -> str:
    return f"{plan.campaign_id}-status-tracker-v{plan.version}.csv"


def tracker_csv(plan: WorkflowPlan, checklist: AssetChecklist) -> bytes:
    decisions = {i.asset_id: i for i in checklist.items}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["asset_id", "asset_type", "decision", "status", "draft_due", "review_due",
         "confirm_due", "review_gate"]
    )
    for entry in plan.entries:
        item = decisions.get(entry.asset_id)
        writer.writerow(
            [
                entry.asset_id,
                entry.asset_type,
                item.decision if item else "",
                item.status if item else "planned",
                entry.draft_due,
                entry.review_due,
                entry.confirm_due,
                entry.review_gate,
            ]
        )
    return buffer.getvalue().encode("utf-8")
