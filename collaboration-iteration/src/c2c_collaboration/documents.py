"""Deterministic document rendering — no LLM in this module.

A revision round produces a NEW versioned Word document (additive workspace —
overwrites impossible) carrying the revised sections, the preserved claim-marker
provenance table, and the per-round edit summary so reviewers verify at a glance.
Real Word tracked changes arrive with the Graph binding; the dev binding is the
new-version + change-log form approved in the plan."""

from __future__ import annotations

from c2c_content_repurposing.models import StagedDraft
from shiftai_shared.m365.word import DocSection, DocSpec, build_docx

from c2c_collaboration.models import ItemResolution, ReviewRound


def revised_filename(campaign_slug: str, asset_type: str, version: int) -> str:
    return f"{campaign_slug}-{asset_type.replace('_', '-')}-v{version}.docx"


def revised_docx(draft: StagedDraft, round_: ReviewRound) -> bytes:
    sections: list[DocSection] = [
        DocSection(heading=s.heading, paragraphs=tuple(s.paragraphs)) for s in draft.sections
    ]
    if draft.claim_markers:
        sections.append(
            DocSection(
                heading="Claim provenance (inline markers — protected)",
                table_header=("Marker", "Claim — source"),
                table_rows=tuple(
                    (m.marker, f"{m.claim} — {m.source_ref}") for m in draft.claim_markers
                ),
            )
        )
    sections.append(
        DocSection(
            heading=f"Edit summary — review round {round_.round}",
            paragraphs=(round_.edit_summary or "No textual edits were applied this round.",),
            table_header=("Feedback item", "Outcome"),
            table_rows=tuple(
                (r.feedback_id, f"{r.outcome}{f' — {r.note}' if r.note else ''}")
                for r in round_.resolutions
            ),
        )
    )
    if round_.marker_violations:
        sections.append(
            DocSection(
                heading="Protected sections (restored — sourced claims are human-only edits)",
                paragraphs=tuple(round_.marker_violations),
            )
        )
    subtitle = (
        f"Campaign {draft.campaign_id} · {draft.asset_type} · REVISION v{draft.version}"
        f" · round {round_.round} · staged for re-review — content_confirmed is a human action"
    )
    return build_docx(DocSpec(title=draft.title, subtitle=subtitle, sections=tuple(sections)))


def resolution_lines(resolutions: list[ItemResolution]) -> list[str]:
    return [f"{r.feedback_id}: {r.outcome}" + (f" ({r.note})" if r.note else "")
            for r in resolutions]
