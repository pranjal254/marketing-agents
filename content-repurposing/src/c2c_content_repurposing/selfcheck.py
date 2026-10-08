"""Generation-time self-check per asset (spec step 8) — deterministic, no LLM.

The Quality Gate remains the authority; this check keeps its failure rate low.
Failure conditions (any → the draft is NOT staged as-is; the caller regenerates
up to the configured limit, then withholds the asset with a gap note):
- a brand-rules lint ERROR (terminology, banned terms, urgency/fear, BC/F&O,
  Copilot-cloud-only) — warnings are advisory and pass through to reviewers;
- a numeric/statistic token whose digits appear in no cited sourced claim;
- a mustNameBrand recipe (FAQ/AEO) whose text never names the active brand
  (from the rules pack — LevelShift, DemandBlue, …) explicitly;
- a word count outside the range the campaign's content settings ask for.

Length is checked here rather than at the Quality Gate on purpose. A model that
came back short or long needs regenerating, which this loop already does, and
catching it here means a human reviewer never opens a draft that is obviously
the wrong size.
"""

from __future__ import annotations

from shiftai_shared.brand import BrandRules, lint_text

from c2c_content_repurposing.models import SelfCheckReport


def count_words(text: str) -> int:
    return len(text.split())


def run_self_check(
    text: str,
    rules: BrandRules,
    *,
    unsourced_numeric_tokens: list[str],
    must_name_brand: bool = False,
    attempts: int = 1,
    word_range: tuple[int, int] | None = None,
) -> SelfCheckReport:
    findings = [
        {"rule_id": f.rule_id, "severity": f.severity, "term": f.term, "detail": f.detail}
        for f in lint_text(text, rules)
    ]
    errors = [f for f in findings if f["severity"] == "error"]
    # The brand comes from the ACTIVE rules pack — never hardcoded: under the
    # DemandBlue pack a FAQ naming DemandBlue satisfies the AEO rule.
    missing_brand = must_name_brand and rules.brand_name.lower() not in text.lower()

    words = count_words(text)
    in_range = True
    if word_range is not None:
        low, high = word_range
        in_range = low <= words <= high

    passed = not errors and not unsourced_numeric_tokens and not missing_brand and in_range
    return SelfCheckReport(
        passed=passed,
        attempts=attempts,
        findings=findings,
        unsourced_numeric_tokens=list(unsourced_numeric_tokens),
        missing_brand_mention=missing_brand,
        word_count=words,
        word_range=word_range,
        word_count_in_range=in_range,
    )


def failure_feedback(report: SelfCheckReport) -> list[str]:
    """Human-readable failure codes handed back to the model on regeneration."""
    feedback = [
        f"{f['rule_id']}: {f['term']}: {f['detail']}"
        for f in report.findings
        if f["severity"] == "error"
    ]
    if report.unsourced_numeric_tokens:
        feedback.append(
            "unsourced_numeric: these figures appear in no cited claim; remove them or "
            "cite the inventory item that contains them: "
            + ", ".join(report.unsourced_numeric_tokens)
        )
    if report.missing_brand_mention:
        feedback.append(
            "missing_brand_mention: the FAQ/AEO derivative must name the brand "
            "explicitly in answer-extractable text"
        )
    if not report.word_count_in_range and report.word_range and report.word_count is not None:
        low, high = report.word_range
        direction = "short" if report.word_count < low else "long"
        # Say which way to move and by roughly how much: "rewrite to length" on
        # its own tends to produce another draft the same size.
        shortfall = low - report.word_count if direction == "short" else report.word_count - high
        feedback.append(
            f"word_count_out_of_range: the draft is {report.word_count} words, which is "
            f"{shortfall} too {direction}. This campaign asks for {low} to {high} words. "
            + (
                "Develop the existing points further rather than adding new claims."
                if direction == "short"
                else "Tighten the prose rather than dropping a required section."
            )
        )
    return feedback
