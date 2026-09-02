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


def pack_filename(pack: AudienceOfferPack) -> str:
    return f"{pack.campaign_id}-audience-offer-pack-v{pack.version}.docx"


def pack_json_filename(pack: AudienceOfferPack) -> str:
    return f"{pack.campaign_id}-audience-offer-pack-v{pack.version}.json"


def pack_docx(pack: AudienceOfferPack, checklist: AssetChecklist | None = None) -> bytes:
    sections: list[DocSection] = [
        DocSection(
            heading="Audience",
            paragraphs=(
                f"Vertical: {pack.vertical}",
                *(f"{seg}: {rat}" for seg, rat in pack.segment_applicability.items()),
                *(f"Exclusion: {e}" for e in pack.exclusions),
            ),
        ),
        DocSection(
            heading="Personas",
            table_header=("Persona", "Role pains / grounding"),
            table_rows=tuple(
                (p.title, f"{p.role_pains} — {p.rationale}") for p in pack.personas
            ),
        ),
        DocSection(
            heading="Offer framing",
            paragraphs=(
                f"Value proposition: {pack.value_proposition}",
                *(f"Differentiator: {d}" for d in pack.differentiators),
            ),
            table_header=("Funnel stage", "CTA"),
            table_rows=tuple(pack.ctas.items()),
        ),
        DocSection(
            heading="Proof points (per-claim provenance)",
            table_header=("Claim", "Source"),
            table_rows=tuple((p.claim, p.source_ref) for p in pack.proof_points),
        ),
        DocSection(
            heading="Messaging angles",
            table_header=("Persona", "Angle / grounding"),
            table_rows=tuple(
                (a.persona_id, f"{a.angle} — {a.grounding}") for a in pack.messaging_angles
            ),
        ),
        DocSection(
            heading="Channel emphasis",
            table_header=("Channel", "Rationale"),
            table_rows=tuple(pack.channel_emphasis.items()),
        ),
    ]
    if pack.gaps:
        sections.append(
            DocSection(heading="Open gaps (explicit — never filled with plausible content)",
                       paragraphs=tuple(pack.gaps))
        )
    if checklist is not None:
        sections.append(
            DocSection(
                heading="Asset checklist (reuse / adapt / create)",
                table_header=("Asset", "Decision — rationale"),
                table_rows=tuple(
                    (
                        f"{i.label} ({i.asset_type})",
                        f"{i.decision}"
                        + (" [reuse-check pending]" if i.reuse_check_pending else "")
                        + f" — {i.decision_rationale}",
                    )
                    for i in checklist.items
                ),
            )
        )
    subtitle = (
        f"Campaign {pack.campaign_id} · pack v{pack.version} · intel mode: {pack.intel_mode}"
        f" · template {pack.template_version} · PROPOSAL — takes effect only after"
        " Marketing Lead confirmation"
    )
    return build_docx(
        DocSpec(title="Audience & Offer Pack", subtitle=subtitle, sections=tuple(sections))
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
