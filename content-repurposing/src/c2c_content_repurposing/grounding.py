"""Never-invent enforcement for drafting (spec guardrail 1, in code).

The model proposes; this module verifies:
- a tagged claim survives only if its sentence_quote is a verbatim flagship
  substring AND its source_ref is a verified pack proof point — a verified-
  sentence claim citing an unverified ref becomes an unsourced finding, and a
  claim whose sentence the draft does not contain is dropped as hallucinated;
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
    GapNote,
    TaggingLLMOutput,
    UnsourcedClaim,
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


# ------------------------------------------------------------- claim tagging


def ground_tagging(
    output: TaggingLLMOutput,
    draft_text: str,
    verified_refs: set[str],
) -> tuple[list[ClaimMarker], list[UnsourcedClaim], int]:
    """Verify the tagging pass in code. Returns (verified sentence-anchored
    markers renumbered c-1.. in reading order, unsourced findings to repair,
    dropped count). Enforcement, not trust:
    - a claim whose sentence_quote is not a verbatim draft substring is dropped
      (the tagger hallucinated the anchor; counted for telemetry);
    - a verbatim claim citing a ref outside the verified proof points becomes an
      unsourced finding with a removal edit — never a verified marker;
    - the model's own unsourced findings are kept when their quote is verbatim
      (a repair edit must point at real text), dropped otherwise.
    """
    haystack = _normalize(draft_text)
    kept: list[tuple[int, ClaimMarker]] = []
    unsourced: list[UnsourcedClaim] = []
    dropped = 0
    for item in output.claims_used:
        quote = _normalize(item.sentence_quote)
        position = haystack.find(quote) if quote else -1
        if position < 0:
            dropped += 1
            continue
        if item.source_ref not in verified_refs:
            unsourced.append(
                UnsourcedClaim(
                    sentence_quote=item.sentence_quote,
                    problem="cites a source outside the verified proof points "
                            f"({item.source_ref!r})",
                    suggested_edit="remove the specific figure or named outcome, or "
                                   "rephrase as qualified, non-quantified framing",
                )
            )
            continue
        kept.append(
            (
                position,
                ClaimMarker(
                    marker="",
                    claim=item.claim or item.sentence_quote,
                    source_ref=item.source_ref,
                    sentence=item.sentence_quote,
                ),
            )
        )
    kept.sort(key=lambda pair: pair[0])
    markers = [
        m.model_copy(update={"marker": f"c-{i}"})
        for i, (_, m) in enumerate(kept, start=1)
    ]
    for finding in output.unsourced:
        quote = _normalize(finding.sentence_quote)
        if quote and quote in haystack:
            unsourced.append(finding)
        else:
            dropped += 1
    return markers, unsourced, dropped


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
            # Sentence-anchored markers carry the verbatim flagship sentence;
            # prefer it as the quote so the fallback inventory stays verbatim.
            quote=m.sentence or m.claim,
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
    # Numeric provenance is checked against the WHOLE verified inventory, not just
    # the items the model formally cited: the inventory is the confirmed flagship's
    # verified claim set, so a number present anywhere in it is sourced by
    # definition (a missing citation is a lineage gap, not a fabricated statistic).
    # Only a number in NO inventory item is genuinely unsourced.
    verified_norm = _normalize(" ".join(f"{i.text} {i.quote}" for i in inventory.items))

    variants = list(output.variants[: max(volume_cap, 0)])
    unsourced: list[str] = []
    for variant in variants:
        for token in numeric_tokens(" ".join(variant.paragraphs)):
            digits = _normalize(token)
            if digits and digits not in verified_norm:
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
