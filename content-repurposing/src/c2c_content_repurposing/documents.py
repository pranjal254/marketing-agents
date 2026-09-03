"""Deterministic document rendering — no LLM in this module.

Drafts are materialized as Word documents with the inline [c-N] markers kept in
the prose and a claim-provenance table at the end (the dev binding of spec step
3's "inline source markers as document comments/metadata"; real Word comments
arrive with the Graph/OneDrive binding). A sidecar claim-map JSON rides next to
every draft so the Quality Gate and Agent 4 read lineage without parsing docx.
"""

from __future__ import annotations

import json

from shiftai_shared.m365.word import DocSection, DocSpec, build_docx

from c2c_content_repurposing.models import ClaimInventory, StagedDraft


def draft_filename(campaign_slug: str, asset_type: str, version: int) -> str:
    """Same naming convention the packaging module validates:
    ``{campaign_slug}-{asset_type}-v{version}`` (Agent 2 config authority)."""
    return f"{campaign_slug}-{asset_type.replace('_', '-')}-v{version}.docx"


def claim_map_filename(draft_filename: str) -> str:
    """Sidecar name derived from the draft's own filename (…-vN.docx → …-vN.claims.json)."""
    return draft_filename.removesuffix(".docx") + ".claims.json"


def draft_docx(draft: StagedDraft) -> bytes:
    sections: list[DocSection] = [
        DocSection(heading=s.heading, paragraphs=tuple(s.paragraphs)) for s in draft.sections
    ]
    if draft.claim_markers:
        sections.append(
            DocSection(
                heading="Claim provenance (inline markers)",
                table_header=("Marker", "Claim — source"),
                table_rows=tuple(
                    (m.marker, f"{m.claim} — {m.source_ref}") for m in draft.claim_markers
                ),
            )
        )
    if draft.claim_lineage:
        sections.append(
            DocSection(
                heading="Claim lineage (flagship inventory items used)",
                paragraphs=(", ".join(draft.claim_lineage),),
            )
        )
    if draft.gap_notes:
        sections.append(
            DocSection(
                heading="Gap notes (not drafted — evidence needed, never invented)",
                table_header=("Section", "What is needed"),
                table_rows=tuple((g.section, g.needed) for g in draft.gap_notes),
            )
        )
    subtitle = (
        f"Campaign {draft.campaign_id} · {draft.asset_type} · DRAFT v{draft.version}"
        f" · staged for human review — the Content Repurposing Agent never publishes"
    )
    return build_docx(DocSpec(title=draft.title, subtitle=subtitle, sections=tuple(sections)))


def claim_map_json(draft: StagedDraft) -> bytes:
    payload = {
        "campaign_id": draft.campaign_id,
        "asset_id": draft.asset_id,
        "asset_type": draft.asset_type,
        "version": draft.version,
        "claim_markers": [m.model_dump() for m in draft.claim_markers],
        "claim_lineage": draft.claim_lineage,
        "gap_notes": [g.model_dump() for g in draft.gap_notes],
        "self_check": draft.self_check.model_dump(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def inventory_filename(campaign_slug: str, flagship_version: int) -> str:
    return f"{campaign_slug}-claim-inventory-v{flagship_version}.json"


def inventory_json(inventory: ClaimInventory) -> bytes:
    return json.dumps(inventory.model_dump(), ensure_ascii=False, indent=2).encode("utf-8")


def flagship_plain_text(draft: StagedDraft) -> str:
    """The text the inventory extraction reads — headings + prose, markers kept."""
    parts: list[str] = [draft.title]
    for section in draft.sections:
        parts.append(section.heading)
        parts.extend(section.paragraphs)
    return "\n".join(parts)
