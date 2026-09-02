"""Never-invent enforcement (grounding) and deterministic back-planning."""

from __future__ import annotations

from datetime import date

from shiftai_shared.brand import load_brand_rules
from shiftai_shared.context_store import InMemoryContextStore

from c2c_campaign_box.agent_config import OrchestratorConfig
from c2c_campaign_box.calendar import add_business_days, back_plan
from c2c_campaign_box.grounding import (
    ground_outlines,
    ground_pack,
    ground_reuse_items,
)
from c2c_campaign_box.intake import load_approved_brief
from c2c_campaign_box.models import (
    AssetChecklistItem,
    ContentOutline,
    IntelBundle,
    IntelSignal,
    OutlineSection,
    PackLLMOutput,
    ProofPoint,
    RepoCandidate,
    ReuseOutlineItem,
)
from tests.conftest import CAMPAIGN_ID, seed_approved_brief


def _bundle() -> IntelBundle:
    return IntelBundle(
        topic="erp",
        mode="intel_library_only",
        signals=[
            IntelSignal(
                signal_id="intel-file-0",
                origin="intel_library",
                kind="file",
                summary="trend file",
                source_uri="/library/trends.md",
                retrieved_at="2026-09-02T00:00:00Z",
            )
        ],
    )


def _brief() -> object:
    store = InMemoryContextStore()
    seed_approved_brief(store)
    return load_approved_brief(store, CAMPAIGN_ID)


def test_unsourced_proof_points_are_excluded_and_become_gaps() -> None:
    brief = _brief()
    output = PackLLMOutput(
        proof_points=[
            ProofPoint(claim="sourced", source_ref="/library/trends.md"),
            ProofPoint(claim="also sourced via brief", source_ref="brief:objective"),
            ProofPoint(claim="fabricated 42% market share", source_ref="https://nowhere"),
        ],
        confidence=0.8,
    )
    grounded, excluded, share, _ = ground_pack(output, _bundle(), brief, load_brand_rules())  # type: ignore[arg-type]
    assert [p.claim for p in grounded.proof_points] == ["sourced", "also sourced via brief"]
    assert len(excluded) == 1 and excluded[0].status == "unverified"
    assert share == round(1 / 3, 4)
    assert any("fabricated 42%" in g for g in grounded.gaps)


def test_pack_language_is_linted() -> None:
    brief = _brief()
    output = PackLLMOutput(value_proposition="Act now — Shift AI delivers!", confidence=0.9)
    _, _, _, findings = ground_pack(output, _bundle(), brief, load_brand_rules())  # type: ignore[arg-type]
    rule_ids = {f["rule_id"] for f in findings}
    assert {"urgency_fear", "shiftai_one_word"} <= rule_ids


def _skeleton(candidates: list[RepoCandidate]) -> list[AssetChecklistItem]:
    return [
        AssetChecklistItem(
            asset_id="faq_service_page",
            asset_type="faq_service_page",
            label="FAQ",
            decision="create",
            decision_rationale="pending",
            candidates_evaluated=candidates,
        )
    ]


def test_reuse_citing_unevaluated_asset_is_demoted_to_create() -> None:
    candidates = [RepoCandidate(asset_ref="/repo/faq.docx", title="faq", fitness_score=0.9)]
    items = [
        ReuseOutlineItem(
            asset_id="faq_service_page",
            decision="reuse",
            rationale="looks good",
            reuse_ref="/somewhere/else.docx",
        )
    ]
    grounded = ground_reuse_items(
        items, {}, _skeleton(candidates), search_performed=True, verified_refs=set()
    )
    assert grounded[0].decision == "create"
    assert grounded[0].reuse_ref is None
    assert "never-invent" in grounded[0].decision_rationale


def test_no_search_forces_create_with_pending_flag() -> None:
    items = [
        ReuseOutlineItem(asset_id="faq_service_page", decision="reuse",
                         rationale="x", reuse_ref="/repo/faq.docx")
    ]
    grounded = ground_reuse_items(
        items, {}, _skeleton([]), search_performed=False, verified_refs=set()
    )
    assert grounded[0].decision == "create"
    assert grounded[0].reuse_check_pending is True


def test_outline_claims_outside_verified_refs_are_stripped() -> None:
    checklist = _skeleton([])
    items = [
        ReuseOutlineItem(
            asset_id="faq_service_page",
            decision="create",
            rationale="x",
            outline=ContentOutline(
                asset_id="faq_service_page",
                asset_type="faq_service_page",
                title="t",
                sections=[
                    OutlineSection(
                        heading="h", notes="n",
                        planned_claims=["brief:objective", "made-up"],
                    )
                ],
            ),
        )
    ]
    outlines = ground_outlines(items, checklist, {"brief:objective"})
    assert outlines[0].sections[0].planned_claims == ["brief:objective"]


# ------------------------------------------------------------------- calendar


def _items(config: OrchestratorConfig) -> list[AssetChecklistItem]:
    return [
        AssetChecklistItem(
            asset_id=c.asset_type, asset_type=c.asset_type, label=c.label,
            decision="create", decision_rationale="x",
        )
        for c in config.required_items()
    ]


def test_business_day_math_skips_weekends() -> None:
    assert add_business_days(date(2026, 10, 15), -3) == date(2026, 10, 12)  # Thu → Mon
    assert add_business_days(date(2026, 10, 12), 1) == date(2026, 10, 13)


def test_feasible_window_back_plans_all_gates(config: OrchestratorConfig) -> None:
    plan = back_plan(
        config, _items(config), campaign_id="c", window_start="2026-10-15",
        window_end="2026-12-15", plan_date=date(2026, 9, 2),
        existing_researched_blog_months=[],
    )
    assert plan.feasible
    flagship = next(e for e in plan.entries if e.review_gate == "flagship")
    derivative = next(e for e in plan.entries if e.review_gate == "derivative")
    # Flagship confirms before derivative drafting starts (flagship-first).
    assert flagship.confirm_due < derivative.draft_due
    assert "review" in flagship.constraint_chain
    assert derivative.confirm_due == "2026-10-15"


def test_too_short_window_reports_trade_offs_never_compresses(
    config: OrchestratorConfig,
) -> None:
    plan = back_plan(
        config, _items(config), campaign_id="c", window_start="2026-09-04",
        window_end="2026-10-01", plan_date=date(2026, 9, 2),
        existing_researched_blog_months=[],
    )
    assert not plan.feasible
    assert plan.infeasibility is not None
    assert plan.infeasibility.trade_offs, "explicit trade-offs required"
    # Gates stay full length: derivative review still spans the configured days.
    derivative = next(e for e in plan.entries if e.review_gate == "derivative")
    assert derivative.draft_due < derivative.confirm_due


def test_capacity_rule_flags_third_blog_in_month(config: OrchestratorConfig) -> None:
    plan = back_plan(
        config, _items(config), campaign_id="c", window_start="2026-10-15",
        window_end="2026-12-15", plan_date=date(2026, 9, 2),
        existing_researched_blog_months=["2026-10", "2026-10"],  # already at cap 2
    )
    assert not plan.feasible
    assert plan.infeasibility is not None
    assert any("capacity" in r for r in plan.infeasibility.reasons)
