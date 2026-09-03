"""Generation-time self-check per asset (spec step 8) — deterministic, no LLM.

The Quality Gate remains the authority; this check keeps its failure rate low.
Failure conditions (any → the draft is NOT staged as-is; the caller regenerates
up to the configured limit, then withholds the asset with a gap note):
- a brand-rules lint ERROR (terminology, banned terms, urgency/fear, BC/F&O,
  Copilot-cloud-only) — warnings are advisory and pass through to reviewers;
- a numeric/statistic token whose digits appear in no cited sourced claim;
- a mustNameBrand recipe (FAQ/AEO) whose text never names LevelShift explicitly.
"""

from __future__ import annotations

from shiftai_shared.brand import BrandRules, lint_text

from c2c_content_repurposing.models import SelfCheckReport

BRAND_NAME = "LevelShift"


def run_self_check(
    text: str,
    rules: BrandRules,
    *,
    unsourced_numeric_tokens: list[str],
    must_name_brand: bool = False,
    attempts: int = 1,
) -> SelfCheckReport:
    findings = [
        {"rule_id": f.rule_id, "severity": f.severity, "term": f.term, "detail": f.detail}
        for f in lint_text(text, rules)
    ]
    errors = [f for f in findings if f["severity"] == "error"]
    missing_brand = must_name_brand and BRAND_NAME.lower() not in text.lower()
    passed = not errors and not unsourced_numeric_tokens and not missing_brand
    return SelfCheckReport(
        passed=passed,
        attempts=attempts,
        findings=findings,
        unsourced_numeric_tokens=list(unsourced_numeric_tokens),
        missing_brand_mention=missing_brand,
    )


def failure_feedback(report: SelfCheckReport) -> list[str]:
    """Human-readable failure codes handed back to the model on regeneration."""
    feedback = [
        f"{f['rule_id']}: {f['term']} — {f['detail']}"
        for f in report.findings
        if f["severity"] == "error"
    ]
    if report.unsourced_numeric_tokens:
        feedback.append(
            "unsourced_numeric: these figures appear in no cited claim — remove them or "
            "cite the inventory item that contains them: "
            + ", ".join(report.unsourced_numeric_tokens)
        )
    if report.missing_brand_mention:
        feedback.append(
            "missing_brand_mention: the FAQ/AEO derivative must name LevelShift "
            "explicitly in answer-extractable text"
        )
    return feedback
