"""Step 2: the deterministic rules pass — exact, reproducible, cheap, no LLM.

Sources of truth: the shared brand rules pack (terminology, banned/discouraged
terms, urgency/fear phrasing — the same lint every agent self-checks against) plus
the gate-only mechanical rules (naming convention vs the manifest's canonical
name, AEO named-mention presence, claim-marker resolution). Severity policy:
terminology/banned/urgency and unresolved markers are governance rules →
blocking; overuse/avoid words and naming drift are style/process → advisory
(guardrail 1). Every finding carries a location anchor + verbatim quote."""

from __future__ import annotations

import re

from shiftai_shared.brand import BrandRules, lint_text

from c2c_quality_gate.agent_config import QualityGateConfig
from c2c_quality_gate.models import Finding, Severity

# Two legitimate marker vocabularies: [c-N] (flagship claim markers) and [cl-N]
# (claim-lineage ids derivatives cite from the confirmed flagship's inventory).
_MARKER = re.compile(r"\[(cl?-\d+)\]")

# Brand-lint rule ids → gate severity. Blocking is reserved for documented
# governance rules; playbook style preferences stay advisory.
_LINT_SEVERITY: dict[str, Severity] = {
    "shiftai_one_word": "blocking",
    "copilot_d365_cloud_only": "blocking",
    "bc_fo_independent": "blocking",
    "banned_term": "blocking",
    "urgency_fear": "blocking",
    "overuse_term": "advisory",
    "avoid_term": "advisory",
}


def _locate(text: str, term: str) -> tuple[str, str]:
    """(location, quote) for the first occurrence of ``term`` — line-anchored,
    quoting the surrounding line verbatim. Never invents a position."""
    lower = text.lower()
    idx = lower.find(term.lower())
    if idx < 0:
        return "", ""
    line_no = text.count("\n", 0, idx) + 1
    line_start = text.rfind("\n", 0, idx) + 1
    line_end = text.find("\n", idx)
    line = text[line_start : line_end if line_end >= 0 else len(text)].strip()
    return f"line {line_no}", line[:300]


def lint_findings(text: str, rules: BrandRules) -> list[Finding]:
    """The shared brand lint, mapped to gate findings with locations."""
    out: list[Finding] = []
    for hit in lint_text(text, rules):
        location, quote = _locate(text, hit.term)
        out.append(
            Finding(
                rule_id=hit.rule_id,
                severity=_LINT_SEVERITY.get(hit.rule_id, "advisory"),
                source="deterministic",
                location=location,
                quote=quote or hit.term,
                quote_verified=bool(quote),
                reasoning=hit.detail,
                remediation=f"Remove or replace {hit.term!r} per the brand rules pack.",
            )
        )
    return out


def naming_finding(filename: str, canonical_name: str) -> Finding | None:
    """Naming-convention check against the manifest's canonical name (packaging
    already auto-corrects; residual drift is advisory for the reviewer)."""
    stem = filename.rsplit(".", 1)[0]
    canonical_stem = canonical_name.rsplit(".", 1)[0]
    if stem == canonical_stem:
        return None
    return Finding(
        rule_id="naming_convention",
        severity="advisory",
        source="deterministic",
        location="filename",
        quote=filename,
        reasoning=f"Filename differs from the canonical name {canonical_name!r}.",
        remediation="Align the filename with the versioned naming template.",
    )


def aeo_named_mention_finding(
    text: str, asset_type: str, brand_name: str, config: QualityGateConfig
) -> Finding | None:
    """AEO rule: FAQ/service-page assets must name the brand in answer-extractable
    text (spec step 2). Documented governance rule → blocking when absent."""
    if asset_type not in config.aeo_named_mention_asset_types:
        return None
    if re.search(rf"\b{re.escape(brand_name)}\b", text, re.IGNORECASE):
        return None
    return Finding(
        rule_id="aeo_named_mention",
        severity="blocking",
        source="deterministic",
        location="whole asset",
        quote="",
        quote_verified=False,
        reasoning=f"No named mention of {brand_name!r} in an AEO-extractable asset.",
        remediation=f"Name {brand_name} explicitly in answer-extractable text.",
    )


def marker_findings(text: str, resolved_markers: set[str]) -> list[Finding]:
    """Step 4 (mechanical half): every inline [c-N] marker must resolve to the
    campaign's verified claim set. Unresolved markers are unsourced claims —
    blocking, fail-closed."""
    out: list[Finding] = []
    for marker in sorted(set(_MARKER.findall(text))):
        if marker in resolved_markers:
            continue
        location, quote = _locate(text, f"[{marker}]")
        out.append(
            Finding(
                rule_id="unsourced_claim",
                severity="blocking",
                source="deterministic",
                location=location,
                quote=quote,
                reasoning=f"Marker [{marker}] does not resolve to a verified claim.",
                remediation="Restore the claim's verified source or remove the claim "
                "via the review cycle — the gate never edits.",
            )
        )
    return out


def markers_in_text(text: str) -> set[str]:
    return set(_MARKER.findall(text))


def run_deterministic(
    text: str,
    *,
    asset_type: str,
    filename: str,
    canonical_name: str,
    resolved_markers: set[str],
    rules: BrandRules,
    config: QualityGateConfig,
) -> list[Finding]:
    findings = lint_findings(text, rules)
    naming = naming_finding(filename, canonical_name)
    if naming is not None:
        findings.append(naming)
    aeo = aeo_named_mention_finding(text, asset_type, rules.brand_name, config)
    if aeo is not None:
        findings.append(aeo)
    findings.extend(marker_findings(text, resolved_markers))
    return findings
