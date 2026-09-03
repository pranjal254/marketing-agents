"""Never-invent enforcement for drafting (spec guardrail 1, in code).

The model proposes; this module verifies:
- a flagship section survives only if every [c-N] marker it uses resolves to a
  ``claims_used`` entry whose source_ref is a verified pack proof point — anything
  else strips the section to a gap note;
- an inventory item survives only if its quote is a verbatim flagship substring
  AND its source_ref is one of the flagship's marker refs;
- a derivative may cite inventory claim_ids only, and any numeric/statistic token
  in its text must appear in a cited inventory item (an unsourced competitor/ROI
  number must be zero — spec Alerting) — else the asset is withheld.
"""

from __future__ import annotations

import re

from c2c_content_repurposing.models import (
    ClaimInventory,
    ClaimInventoryItem,
    ClaimMarker,
    DerivativeLLMOutput,
    DerivativeVariant,
    DraftSection,
    FlagshipLLMOutput,
    GapNote,
)

_MARKER = re.compile(r"\[(c-\d+)\]")
# Statistics/money/multipliers: 42%, $1.2m, 3x (ASCII x or the multiplication sign)
_NUMERIC_CLAIM = re.compile(
    r"\d+(?:\.\d+)?\s?%|\$\s?\d[\d,]*(?:\.\d+)?\s?[kmb]?\b|\b\d+(?:\.\d+)?\s?[x×]\b",  # noqa: RUF001
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def markers_in(text: str) -> set[str]:
    return set(_MARKER.findall(text))


def numeric_tokens(text: str) -> list[str]:
    return [m.strip() for m in _NUMERIC_CLAIM.findall(text)]


# ------------------------------------------------------------- flagship


def ground_flagship(
    output: FlagshipLLMOutput,
    verified_refs: set[str],
    campaign_id: str,
    asset_id: str,
) -> tuple[list[DraftSection], list[ClaimMarker], list[GapNote]]:
    """Returns (surviving sections, verified markers actually used, gap notes for
    everything stripped). Enforcement, not trust:
    - claims_used entries citing an unverified ref are rejected;
    - a section whose text carries a marker without a verified claims_used entry
      is stripped entirely (never partially rewritten by us — we flag, not edit);
    - a section with a bare numeric claim and NO marker at all is stripped too.
    """
    verified_markers = {
        m.marker: m for m in output.claims_used if m.source_ref in verified_refs
    }
    rejected_markers = {m.marker for m in output.claims_used if m.source_ref not in verified_refs}

    sections: list[DraftSection] = []
    used_markers: list[ClaimMarker] = []
    gap_notes: list[GapNote] = []
    seen: set[str] = set()
    gap_seq = 0

    def gap(section: str, needed: str) -> None:
        nonlocal gap_seq
        gap_seq += 1
        gap_notes.append(
            GapNote(
                gap_id=f"gap_{campaign_id}_{asset_id}_{gap_seq}",
                asset_id=asset_id,
                section=section,
                needed=needed,
            )
        )

    for note in output.gap_notes:  # the model's own declared gaps are kept as-is
        gap(note.section, note.needed)

    for section in output.sections:
        text = " ".join(section.paragraphs)
        found = markers_in(text)
        unresolved = {m for m in found if m not in verified_markers}
        if unresolved & rejected_markers:
            gap(
                section.heading,
                "claims cited a source outside the verified proof points: "
                + ", ".join(sorted(unresolved & rejected_markers)),
            )
            continue
        if unresolved:
            gap(
                section.heading,
                "inline markers without a claims_used entry: " + ", ".join(sorted(unresolved)),
            )
            continue
        if not found and numeric_tokens(text):
            gap(
                section.heading,
                "numeric/statistical content with no claim marker — provenance required",
            )
            continue
        sections.append(DraftSection(heading=section.heading, paragraphs=list(section.paragraphs)))
        for marker_id in sorted(found):
            if marker_id not in seen:
                seen.add(marker_id)
                used_markers.append(verified_markers[marker_id])

    return sections, used_markers, gap_notes


# ------------------------------------------------------------- claim inventory


def verify_inventory_items(
    items: list[ClaimInventoryItem],
    flagship_text: str,
    marker_refs: set[str],
) -> tuple[list[ClaimInventoryItem], int]:
    """Keep only items whose quote is a verbatim flagship substring (whitespace-
    normalized) and whose source_ref is a flagship marker ref. Returns
    (verified items with stable ids, dropped count)."""
    haystack = _normalize(flagship_text)
    kept: list[ClaimInventoryItem] = []
    dropped = 0
    for index, item in enumerate(items, start=1):
        quote_ok = bool(item.quote) and _normalize(item.quote) in haystack
        if quote_ok and item.source_ref in marker_refs:
            kept.append(item.model_copy(update={"claim_id": f"cl-{index}"}))
        else:
            dropped += 1
    # Re-number after drops so ids stay dense and deterministic.
    kept = [item.model_copy(update={"claim_id": f"cl-{i}"}) for i, item in enumerate(kept, 1)]
    return kept, dropped


def deterministic_inventory(
    markers: list[ClaimMarker], flagship_version: int, campaign_id: str, created_at: str
) -> ClaimInventory:
    """Fallback when the extraction call degrades: the flagship's own verified
    claim map IS a valid (if minimal) inventory — never invented, always sourced."""
    items = [
        ClaimInventoryItem(
            claim_id=f"cl-{i}",
            kind="claim",
            text=m.claim,
            quote=m.claim,
            source_ref=m.source_ref,
        )
        for i, m in enumerate(markers, start=1)
    ]
    return ClaimInventory(
        campaign_id=campaign_id,
        flagship_version=flagship_version,
        items=items,
        method="deterministic_fallback",
        created_at=created_at,
    )


# ------------------------------------------------------------- derivatives


def ground_derivative(
    output: DerivativeLLMOutput,
    inventory: ClaimInventory,
    *,
    volume_cap: int,
    campaign_id: str,
    asset_id: str,
) -> tuple[list[DerivativeVariant], list[str], list[str], list[GapNote]]:
    """Returns (variants within cap, valid claim lineage, unsourced numeric tokens,
    gap notes). Lineage is claims_used ∩ inventory ids; a numeric token in the text
    that appears in no CITED inventory item is unsourced (spec: must be zero)."""
    inventory_ids = {i.claim_id for i in inventory.items}
    lineage = [c for c in output.claims_used if c in inventory_ids]
    cited_text = " ".join(
        f"{i.text} {i.quote}" for i in inventory.items if i.claim_id in lineage
    )
    cited_norm = _normalize(cited_text)

    variants = list(output.variants[: max(volume_cap, 0)])
    unsourced: list[str] = []
    for variant in variants:
        for token in numeric_tokens(" ".join(variant.paragraphs)):
            digits = _normalize(token)
            if digits and digits not in cited_norm:
                unsourced.append(token)

    gap_notes = [
        GapNote(
            gap_id=f"gap_{campaign_id}_{asset_id}_{i}",
            asset_id=asset_id,
            section=note.section,
            needed=note.needed,
        )
        for i, note in enumerate(output.gap_notes, start=1)
    ]
    return variants, lineage, unsourced, gap_notes
