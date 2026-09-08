"""Step 5: merge the two passes into the per-asset compliance report — grounding
enforced in code, never trusted from the model.

- Contextual severity is POLICY, not model output: severity is reassigned from
  CONTEXTUAL_RULES by rule id; a finding naming an unknown rule is dropped (the
  model may not invent rules — guardrail 4) and counted as an anomaly.
- Quotes are verified verbatim against the asset text; an unverifiable quote is
  kept (fail-closed: over-flagging is the safe direction, and gate_precision
  telemetry watches it) but marked ``quote_verified=False`` for the reviewer.
- The verdict is a pure function of blocking-finding count (spec Explainability).
- Fail-closed: a contextual pass that errored, or that did not explicitly
  complete every contextual rule, yields ``checks_complete=False`` and the asset
  FAILS — deterministic findings still stand in the partial report."""

from __future__ import annotations

from c2c_quality_gate.models import (
    CONTEXTUAL_RULES,
    ContextualLLMOutput,
    Finding,
)


def ground_contextual(
    output: ContextualLLMOutput | None, asset_text: str
) -> tuple[list[Finding], bool, str, int]:
    """→ (findings, checks_complete, incomplete_reason, dropped_unknown_rules)."""
    if output is None:
        return [], False, "contextual pass returned no parsable report", 0
    findings: list[Finding] = []
    dropped = 0
    for raw in output.findings:
        severity = CONTEXTUAL_RULES.get(raw.rule_id)
        if severity is None:
            dropped += 1  # invented rule — never enters the report
            continue
        if _is_non_finding(raw.reasoning) or _is_non_finding(raw.remediation):
            dropped += 1  # the model listed a COMPLIANT item as a finding — drop it
            continue
        quote = raw.quote.strip()
        findings.append(
            Finding(
                rule_id=raw.rule_id,
                severity=severity,
                source="contextual",
                location=raw.location,
                quote=quote,
                quote_verified=bool(quote) and quote in asset_text,
                reasoning=raw.reasoning,
                remediation=raw.remediation,
            )
        )
    completed = {c.strip() for c in output.checks_completed}
    missing = sorted(set(CONTEXTUAL_RULES) - completed)
    if missing:
        return (
            findings,
            False,
            f"contextual rules not completed: {', '.join(missing)}",
            dropped,
        )
    return findings, True, "", dropped


# Phrases a model uses when it lists a COMPLIANT item as if it were a finding.
# Such a "finding" is not a violation and must never block the asset.
_NON_FINDING_PHRASES = (
    "no change needed",
    "no changes needed",
    "already includes",
    "already has",
    "already carries",
    "already compliant",
    "is compliant",
    "not a violation",
    "no violation",
    "compliant as written",
)


def _is_non_finding(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _NON_FINDING_PHRASES)


def verdict_for(findings: list[Finding], checks_complete: bool) -> str:
    """Pure function: fail on any blocking finding OR incomplete checks."""
    if not checks_complete:
        return "fail"
    return "fail" if any(f.severity == "blocking" for f in findings) else "pass"
