"""Never-drop / never-adjudicate / never-weaken enforcement (spec guardrails 2, 3, 5).

The model proposes; this module verifies:
- consolidation output is reconciled against the input — a feedback item the
  model lost comes back as ``deferred`` for human review, never vanishes;
- contradictory items become ConflictRecords with BOTH positions quoted; the
  affected instructions are excluded from the revision call (held, not judged);
- claim→source markers are immutable: every ``[c-N]`` marker and its carrying
  sentence must survive revision verbatim — a violated section is restored from
  the original and the edit is flagged for a human (sourced_claim_edit).
"""

from __future__ import annotations

import re

from c2c_collaboration.models import (
    ConflictPosition,
    ConflictRecord,
    ConsolidationLLMOutput,
    FeedbackItem,
    ItemResolution,
    NormalizedItem,
    RevisedSection,
)

_MARKER = re.compile(r"\[(c-\d+)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def markers_in(text: str) -> set[str]:
    return set(_MARKER.findall(text))


def marker_sentences(paragraphs: list[str]) -> dict[str, str]:
    """marker id → the (normalized) sentence carrying it."""
    out: dict[str, str] = {}
    for paragraph in paragraphs:
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            for marker in _MARKER.findall(sentence):
                out[marker] = _normalize(sentence)
    return out


# ------------------------------------------------------------- consolidation


def reconcile_consolidation(
    items: list[FeedbackItem], output: ConsolidationLLMOutput | None
) -> tuple[list[NormalizedItem], list[str]]:
    """Force the output to cover the input exactly once. Returns (normalized,
    unclassified_ids). Lost items come back typed textual but are resolved as
    ``deferred`` by the caller — visible to humans, never applied blindly."""
    by_id: dict[str, NormalizedItem] = {}
    input_ids = [i.feedback_id for i in items]
    known = set(input_ids)
    for norm in output.items if output else []:
        if norm.feedback_id in known and norm.feedback_id not in by_id:
            by_id[norm.feedback_id] = norm  # hallucinated/duplicate rows dropped
    unclassified: list[str] = []
    normalized: list[NormalizedItem] = []
    for item in items:
        found = by_id.get(item.feedback_id)
        if found is None:
            unclassified.append(item.feedback_id)
            found = NormalizedItem(
                feedback_id=item.feedback_id,
                location=item.section,
                instruction=item.text,
                reviewer=item.reviewer_id,
                type="textual",
                rationale="not classified by the model — needs human review",
            )
        normalized.append(found)
    return normalized, unclassified


def extract_conflicts(
    normalized: list[NormalizedItem],
    items: list[FeedbackItem],
    *,
    campaign_id: str,
    asset_id: str,
    round_n: int,
    created_at: str,
) -> tuple[list[ConflictRecord], set[str]]:
    """Pair up conflicts_with references into ConflictRecords (both positions
    quoted from the reviewers' OWN words). Returns (records, conflicted ids)."""
    by_id = {i.feedback_id: i for i in items}
    seen_pairs: set[tuple[str, str]] = set()
    conflicted: set[str] = set()
    records: list[ConflictRecord] = []
    for norm in normalized:
        other_id = norm.conflicts_with
        if not other_id or other_id not in by_id or other_id == norm.feedback_id:
            continue
        low, high = sorted((norm.feedback_id, other_id))
        pair = (low, high)
        conflicted.add(norm.feedback_id)
        conflicted.add(other_id)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        first, second = by_id[low], by_id[high]
        records.append(
            ConflictRecord(
                conflict_id=f"cf-{round_n}-{len(records) + 1}",
                campaign_id=campaign_id,
                asset_id=asset_id,
                section=norm.location or first.section or second.section,
                positions=[
                    ConflictPosition(reviewer_id=first.reviewer_id,
                                     reviewer_role=first.reviewer_role, quote=first.text),
                    ConflictPosition(reviewer_id=second.reviewer_id,
                                     reviewer_role=second.reviewer_role, quote=second.text),
                ],
                round=round_n,
                created_at=created_at,
            )
        )
    return records, conflicted


# ------------------------------------------------------------- marker shield


def protect_markers(
    original: list[RevisedSection], revised: list[RevisedSection]
) -> tuple[list[RevisedSection], list[str]]:
    """Restore any section where a claim marker or its carrying sentence did not
    survive verbatim. Returns (safe sections, violation notes). Sections the
    original didn't have markers in are free to change; marker sections are
    immutable without a human (spec guardrail 3)."""
    revised_by_heading = {s.heading: s for s in revised}
    safe: list[RevisedSection] = []
    violations: list[str] = []
    for section in original:
        candidate = revised_by_heading.get(section.heading)
        if candidate is None:
            # A dropped section is a destructive change — restore it.
            safe.append(section)
            if markers_in(" ".join(section.paragraphs)):
                violations.append(f"section dropped: {section.heading}")
            continue
        original_sentences = marker_sentences(section.paragraphs)
        if not original_sentences:
            safe.append(candidate)
            continue
        revised_text = _normalize(" ".join(candidate.paragraphs))
        broken = [
            marker for marker, sentence in original_sentences.items()
            if sentence not in revised_text
        ]
        if broken:
            safe.append(section)  # restore the original wholesale — flag, never edit
            violations.append(
                f"{section.heading}: marker sentence(s) altered ({', '.join(sorted(broken))})"
            )
        else:
            safe.append(candidate)
    return safe, violations


# ------------------------------------------------------------- reconciliation


def resolve_items(
    normalized: list[NormalizedItem],
    *,
    conflicted: set[str],
    applied: set[str],
    deferred_reasons: dict[str, str],
    violated_sections: set[str],
) -> list[ItemResolution]:
    """Every feedback item ends with exactly one outcome (spec Fallback):
    conflicted > structural > out_of_scope > flagged (marker shield) > applied
    > deferred. An applied edit that targeted a restored (marker-protected)
    section is flagged for a human, not counted as applied."""
    violated = {_normalize(s).lower() for s in violated_sections}

    def hit_protected(location: str) -> bool:
        loc = _normalize(location).lower()
        # Empty location with violations present → conservative: route to human.
        return bool(violated) and (not loc or any(loc in v or v in loc for v in violated))

    out: list[ItemResolution] = []
    for norm in normalized:
        fid = norm.feedback_id
        if fid in conflicted:
            out.append(ItemResolution(feedback_id=fid, outcome="conflicted",
                                      note="held for the Marketing Lead"))
        elif norm.type == "structural":
            out.append(ItemResolution(feedback_id=fid, outcome="routed_structural",
                                      note=norm.rationale))
        elif norm.type == "out_of_scope":
            out.append(ItemResolution(feedback_id=fid, outcome="logged_backlog",
                                      note="retrospective backlog (sub-process 5)"))
        elif fid in applied and hit_protected(norm.location):
            out.append(ItemResolution(feedback_id=fid, outcome="flagged_sourced_claim",
                                      note="edit touched a sourced-claim sentence — human routing"))
        elif fid in applied:
            out.append(ItemResolution(feedback_id=fid, outcome="applied"))
        else:
            out.append(ItemResolution(
                feedback_id=fid, outcome="deferred",
                note=deferred_reasons.get(fid, "not applied this round — needs human review"),
            ))
    return out
