"""Task 4 — BC/F&O independence rule at intake (deterministic; guardrail 3).

A request mixing Business Central and F&O in one campaign concept is split or
flagged, never silently merged. The agent proposes the split; a human decides.
"""

from __future__ import annotations

import re

from campaign_identification.models import BcFoCheck, CampaignRequest

_BC_PATTERNS = (
    re.compile(r"\bbusiness\s+central\b", re.IGNORECASE),
    re.compile(r"\bbc\b", re.IGNORECASE),
    re.compile(r"\bd365\s*bc\b", re.IGNORECASE),
)
_FO_PATTERNS = (
    re.compile(r"\bf\s*&\s*o\b", re.IGNORECASE),
    re.compile(r"\bfinance\s+(and|&)\s+operations\b", re.IGNORECASE),
    re.compile(r"\bd365\s*f&o\b", re.IGNORECASE),
    re.compile(r"\bfo\b", re.IGNORECASE),
)


def _hits(text: str, patterns: tuple[re.Pattern[str], ...]) -> list[str]:
    return [m.group(0) for p in patterns for m in p.finditer(text)]


_COMPLIANCE_PATTERNS = (
    re.compile(r"\bpricing\b|\bprice\s+list\b|\bdiscount", re.IGNORECASE),
    re.compile(r"\blegal\b|\bcontract(ual)?\b|\bliabilit", re.IGNORECASE),
    re.compile(r"\bpartner\s+commit", re.IGNORECASE),
)


def touches_compliance(request: CampaignRequest) -> list[str]:
    """Authority-envelope compliance ceiling: pricing / legal / partner commitments
    always escalate to a human (spec Human Review Triggers)."""
    text = " ".join(
        filter(None, [request.offer_topic, request.objective, request.free_text_context])
    )
    return sorted({m.group(0).lower() for p in _COMPLIANCE_PATTERNS for m in p.finditer(text)})


def check_bc_fo(request: CampaignRequest) -> BcFoCheck:
    """Detect BC and F&O in the explicit product scope and the request text."""
    products = {p.strip().upper().replace("&", "") for p in request.products}
    has_bc = "BC" in products or "BUSINESS CENTRAL" in {p.upper() for p in request.products}
    has_fo = "FO" in products or "F O" in products

    text = " ".join(
        filter(None, [request.offer_topic, request.objective, request.free_text_context])
    )
    bc_evidence = _hits(text, _BC_PATTERNS)
    fo_evidence = _hits(text, _FO_PATTERNS)
    has_bc = has_bc or bool(bc_evidence)
    has_fo = has_fo or bool(fo_evidence)

    if not (has_bc and has_fo):
        return BcFoCheck(mixed=False)

    evidence = sorted({*bc_evidence, *fo_evidence, *(p for p in request.products)})
    topic = request.offer_topic or "the requested topic"
    return BcFoCheck(
        mixed=True,
        evidence=evidence,
        split_proposal=[
            f"Business Central campaign concept: {topic} (BC scope only)",
            f"F&O campaign concept: {topic} (F&O scope only)",
        ],
    )
