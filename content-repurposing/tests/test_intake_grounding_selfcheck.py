"""Unit tests: intake refusal, never-invent grounding, deterministic self-check."""

from __future__ import annotations

import pytest
from conftest import CAMPAIGN_ID, seed_box_plan
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.context_store import InMemoryContextStore

from c2c_content_repurposing.agent_config import RepurposingConfig
from c2c_content_repurposing.grounding import (
    deterministic_inventory,
    ground_derivative,
    ground_flagship,
    numeric_tokens,
    verify_inventory_items,
)
from c2c_content_repurposing.intake import (
    FlagshipOutlineMissingError,
    PlanNotReadyError,
    load_drafting_context,
)
from c2c_content_repurposing.models import (
    ClaimInventory,
    ClaimInventoryItem,
    ClaimMarker,
    DerivativeLLMOutput,
    DerivativeVariant,
    FlagshipLLMOutput,
    FlagshipLLMSection,
)
from c2c_content_repurposing.selfcheck import failure_feedback, run_self_check

RULES = load_brand_rules()


# ------------------------------------------------------------------ intake


def test_intake_requires_plan_case(config: RepurposingConfig) -> None:
    with pytest.raises(PlanNotReadyError):
        load_drafting_context(InMemoryContextStore(), config, "cmp_missing")


def test_intake_requires_confirmed_plan(config: RepurposingConfig) -> None:
    store = InMemoryContextStore()
    seed_box_plan(store, status="awaiting_confirmation")
    with pytest.raises(PlanNotReadyError):
        load_drafting_context(store, config, CAMPAIGN_ID)


def test_intake_refuses_unverified_sections(
    store: InMemoryContextStore, config: RepurposingConfig
) -> None:
    context = load_drafting_context(store, config, CAMPAIGN_ID)
    # One section verifies (sig:1); the competitor-stat section is refused up front.
    assert len(context.draftable_sections) == 1
    assert len(context.unverified_gap_notes) == 1
    assert "made-up-competitor-stat" in context.unverified_gap_notes[0].needed


def test_intake_requires_flagship_outline(config: RepurposingConfig) -> None:
    store = InMemoryContextStore()
    seed_box_plan(store)
    record = store.get("content_outlines", CAMPAIGN_ID)
    assert record is not None
    outlines = [o for o in record.value["outlines"] if o["asset_type"] != "flagship_blog"]
    store.put("content_outlines", CAMPAIGN_ID, {"outlines": outlines})
    with pytest.raises(FlagshipOutlineMissingError):
        load_drafting_context(store, config, CAMPAIGN_ID)


# ------------------------------------------------------------------ flagship grounding


def _flagship_output() -> FlagshipLLMOutput:
    return FlagshipLLMOutput(
        title="T",
        sections=[
            FlagshipLLMSection(heading="good", paragraphs=["Sourced fact [c-1] holds."]),
            FlagshipLLMSection(heading="bad-ref", paragraphs=["Invented stat [c-2] here."]),
            FlagshipLLMSection(heading="orphan", paragraphs=["Marker [c-9] undefined."]),
            FlagshipLLMSection(heading="bare-number", paragraphs=["Growth of 37% unsourced."]),
            FlagshipLLMSection(heading="prose", paragraphs=["Plain positioning prose."]),
        ],
        claims_used=[
            ClaimMarker(marker="c-1", claim="Sourced fact", source_ref="sig:1"),
            ClaimMarker(marker="c-2", claim="Invented stat", source_ref="not-a-source"),
        ],
        confidence=0.8,
    )


def test_ground_flagship_strips_everything_unverified() -> None:
    sections, markers, gaps = ground_flagship(
        _flagship_output(), {"sig:1"}, "cmp", "flagship_blog"
    )
    assert [s.heading for s in sections] == ["good", "prose"]
    assert [m.marker for m in markers] == ["c-1"]
    stripped = {g.section for g in gaps}
    assert stripped == {"bad-ref", "orphan", "bare-number"}


def test_numeric_token_patterns() -> None:
    text = "Grew 42%, saved $1.2m, moved 3x faster, shipped 4 features"
    tokens = numeric_tokens(text)
    assert any("42" in t for t in tokens)
    assert any("$" in t for t in tokens)
    assert any("3" in t and "x" in t.lower() for t in tokens)
    assert not any(t.strip() == "4" for t in tokens)  # plain counts are not statistics


# ------------------------------------------------------------------ inventory


def test_inventory_verbatim_quotes_only() -> None:
    flagship_text = "Manufacturers report 42% faster onboarding after modernization."
    items = [
        ClaimInventoryItem(claim_id="a", kind="data_point",
                           text="42% faster onboarding",
                           quote="Manufacturers report 42% faster onboarding",
                           source_ref="sig:1"),
        ClaimInventoryItem(claim_id="b", kind="quote", text="paraphrase",
                           quote="a sentence that is not in the flagship",
                           source_ref="sig:1"),
        ClaimInventoryItem(claim_id="c", kind="claim", text="wrong source",
                           quote="Manufacturers report", source_ref="unknown:ref"),
    ]
    kept, dropped = verify_inventory_items(items, flagship_text, {"sig:1"})
    assert [i.claim_id for i in kept] == ["cl-1"]
    assert dropped == 2


def test_deterministic_inventory_from_markers() -> None:
    markers = [ClaimMarker(marker="c-1", claim="Fact one", source_ref="sig:1")]
    inv = deterministic_inventory(markers, 1, "cmp", "2026-09-03T00:00:00Z")
    assert inv.method == "deterministic_fallback"
    assert inv.items[0].claim_id == "cl-1"
    assert inv.items[0].source_ref == "sig:1"


# ------------------------------------------------------------------ derivatives


def _inventory() -> ClaimInventory:
    return ClaimInventory(
        campaign_id="cmp", flagship_version=1, method="llm_verified",
        items=[
            ClaimInventoryItem(claim_id="cl-1", kind="data_point",
                               text="42% faster onboarding",
                               quote="report 42% faster onboarding", source_ref="sig:1"),
        ],
    )


def test_ground_derivative_caps_volume_and_filters_lineage() -> None:
    output = DerivativeLLMOutput(
        title="d",
        variants=[DerivativeVariant(label=f"v{i}", paragraphs=["Calm text."]) for i in range(4)],
        claims_used=["cl-1", "cl-404"],
    )
    variants, lineage, unsourced, _ = ground_derivative(
        output, _inventory(), volume_cap=2, campaign_id="cmp", asset_id="linkedin_posts"
    )
    assert len(variants) == 2
    assert lineage == ["cl-1"]
    assert unsourced == []


def test_ground_derivative_flags_unsourced_numbers() -> None:
    output = DerivativeLLMOutput(
        title="d",
        variants=[DerivativeVariant(label="v", paragraphs=["We grew 87% last year."])],
        claims_used=["cl-1"],
    )
    _, _, unsourced, _ = ground_derivative(
        output, _inventory(), volume_cap=1, campaign_id="cmp", asset_id="x"
    )
    assert unsourced and "87" in unsourced[0]


def test_ground_derivative_accepts_cited_numbers() -> None:
    output = DerivativeLLMOutput(
        title="d",
        variants=[
            DerivativeVariant(label="v", paragraphs=["Customers see 42% faster onboarding."])
        ],
        claims_used=["cl-1"],
    )
    _, _, unsourced, _ = ground_derivative(
        output, _inventory(), volume_cap=1, campaign_id="cmp", asset_id="x"
    )
    assert unsourced == []


# ------------------------------------------------------------------ self-check


def test_selfcheck_brand_error_fails() -> None:
    report = run_self_check(
        "Shift AI delivers for Business Central and F&O together",
        RULES, unsourced_numeric_tokens=[],
    )
    assert not report.passed
    rule_ids = {f["rule_id"] for f in report.findings}
    assert "shiftai_one_word" in rule_ids
    assert "bc_fo_independent" in rule_ids


def test_selfcheck_unsourced_numeric_fails_and_feedback_names_it() -> None:
    report = run_self_check("Fine prose", RULES, unsourced_numeric_tokens=["87%"])
    assert not report.passed
    assert any("87%" in line for line in failure_feedback(report))


def test_selfcheck_brand_mention_gate() -> None:
    missing = run_self_check("An answer without the brand.", RULES,
                             unsourced_numeric_tokens=[], must_name_brand=True)
    named = run_self_check("LevelShift delivers this outcome.", RULES,
                           unsourced_numeric_tokens=[], must_name_brand=True)
    assert not missing.passed and missing.missing_brand_mention
    assert named.passed


def test_selfcheck_warnings_do_not_fail() -> None:
    # Overuse/avoid terms are warnings for the reviewer, not generation blockers.
    report = run_self_check("A seamless experience", RULES, unsourced_numeric_tokens=[])
    assert report.passed or all(f["severity"] == "warning" for f in report.findings)
