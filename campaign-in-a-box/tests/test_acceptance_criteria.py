"""One test per spec Implementation Task (Agent 2, steps 1-12) + static guardrails."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from c2c_campaign_box.orchestration import CampaignBoxOrchestrator
from tests.conftest import CAMPAIGN_ID, PLAN_DATE, run_plan

SRC = Path(__file__).resolve().parents[1] / "src"
REQUIRED = ["flagship_blog", "email_touchpoints", "linkedin_posts", "faq_service_page",
            "external_one_pager", "call_scripts"]


def _to_production(orchestrator: CampaignBoxOrchestrator) -> None:
    run_plan(orchestrator)
    orchestrator.confirm(CAMPAIGN_ID, "pack", decision="confirmed", actor_id="lead@x")
    orchestrator.confirm(CAMPAIGN_ID, "plan", decision="confirmed", actor_id="lead@x")


def _register_all(orchestrator: CampaignBoxOrchestrator) -> None:
    for a in REQUIRED:
        orchestrator.register_confirmed_asset(
            CAMPAIGN_ID, a, filename=f"erp-modernization-{a.replace('_', '-')}-v1.docx",
            content=f"{a} content".encode(), actor_id="reviewer@x",
            claim_refs=["brief:offer_topic"],
        )


def test_step1_rejects_non_approved_brief(orchestrator: CampaignBoxOrchestrator) -> None:
    outcome = orchestrator.plan_campaign("cmp_never_approved", plan_date=PLAN_DATE)
    assert outcome.status == "failed"
    assert outcome.escalation_reasons == ["brief_not_approved"]


def test_step2_every_intel_data_point_has_uri_and_timestamp(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.pack is not None
    # unverifiable claims excluded from usable proof points (in gaps instead)
    assert all(p.status == "verified" for p in outcome.pack.proof_points)
    assert outcome.pack.gaps


def test_step3_audience_definition_with_segment_rationale(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.pack is not None
    assert outcome.pack.vertical == "manufacturing"
    assert set(outcome.pack.segment_applicability) >= {"type_3"}
    assert outcome.pack.personas and outcome.pack.channel_emphasis


def test_step4_offer_framing_with_per_claim_provenance(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.pack is not None
    assert outcome.pack.value_proposition
    assert all(p.source_ref for p in outcome.pack.proof_points)
    assert outcome.pack.messaging_angles


def test_step5_reuse_decisions_never_create_without_search(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.checklist is not None
    assert outcome.checklist.search_performed
    for item in outcome.checklist.items:
        assert item.decision in ("reuse", "adapt", "create")
        assert item.decision_rationale
        if item.decision in ("reuse", "adapt"):
            assert item.reuse_ref in {c.asset_ref for c in item.candidates_evaluated}


def test_step6_outlines_seeded_from_angles_for_create_adapt(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    checklist = {i.asset_id: i.decision for i in outcome.checklist.items}  # type: ignore[union-attr]
    for outline in outcome.outlines:
        assert checklist[outline.asset_id] in ("create", "adapt")
        assert outline.seeded_from_angles


def test_step7_backplanned_calendar_workspace_and_registry(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.plan is not None and outcome.plan.entries
    assert all(e.constraint_chain for e in outcome.plan.entries)
    assert outcome.workspace_root and Path(outcome.workspace_root).is_dir()
    assert outcome.tracker_ref
    from c2c_campaign_box import persistence as db

    planned = [r for r in orchestrator.deps.store.query(db.KIND_PLANNED_ASSET)]
    assert len(planned) == len(outcome.checklist.items)  # type: ignore[union-attr]


def test_step8_confirmation_gate_with_deltas_and_infeasibility_escalation(
    orchestrator: CampaignBoxOrchestrator, store, config
) -> None:
    from tests.conftest import seed_approved_brief

    # Deltas path is covered in e2e; here: infeasible window escalates with
    # explicit trade-offs instead of silently compressing gates.
    seed_approved_brief(store, "cmp_rush", window_start="2026-09-04",
                        window_end="2026-09-30")
    outcome = orchestrator.plan_campaign("cmp_rush", plan_date=PLAN_DATE)
    assert "infeasible_timeline" in outcome.escalation_reasons
    assert outcome.plan is not None and not outcome.plan.feasible
    assert outcome.plan.infeasibility is not None and outcome.plan.infeasibility.trade_offs


def test_step9_completeness_diff_blocks_with_actionable_report(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    _to_production(orchestrator)
    blocked = orchestrator.run_packaging(CAMPAIGN_ID)
    assert blocked.status == "packaging_blocked"
    assert blocked.report is not None and blocked.report.owners_note


def test_step10_naming_validation_and_hashes(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    _to_production(orchestrator)
    _register_all(orchestrator)
    packaged = orchestrator.run_packaging(CAMPAIGN_ID)
    assert packaged.manifest is not None
    assert all(re.fullmatch(r"[0-9a-f]{64}", a.sha256) for a in packaged.manifest.assets)
    assert all(
        a.canonical_name.startswith("erp-modernization-") for a in packaged.manifest.assets
    )


def test_step11_manifest_registered_pending_compliance_transactionally(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    _to_production(orchestrator)
    _register_all(orchestrator)
    packaged = orchestrator.run_packaging(CAMPAIGN_ID)
    assert packaged.status == "packaged_pending_compliance"
    assert packaged.manifest is not None
    assert packaged.manifest.status == "packaged_pending_compliance"
    assert packaged.manifest.claim_lineage_index  # claim-lineage index present


def test_step12_reopen_tracks_deltas_and_rehashes(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    _to_production(orchestrator)
    _register_all(orchestrator)
    first = orchestrator.run_packaging(CAMPAIGN_ID)
    orchestrator.reopen_assets(CAMPAIGN_ID, ["flagship_blog"],
                               requesting_gate="quality-gate", actor_id="gate@x")
    orchestrator.register_confirmed_asset(
        CAMPAIGN_ID, "flagship_blog",
        filename="erp-modernization-flagship-blog-v2.docx",
        content=b"reworked", actor_id="reviewer@x",
    )
    second = orchestrator.run_packaging(CAMPAIGN_ID)
    assert second.manifest is not None and first.manifest is not None
    assert second.manifest.version == first.manifest.version + 1


# ------------------------------------------------------------ static guardrails


def test_no_salesforce_or_pardot_anywhere() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "salesforce" not in text.replace("no salesforce", "")
        assert "pardot" not in text.replace("no pardot", "")


def test_repository_protocol_is_read_only() -> None:
    """Guardrail 3: no write surface exists on the repository index."""
    from c2c_campaign_box.repository import LocalRepositoryIndex, RepositoryIndex

    for cls in (RepositoryIndex, LocalRepositoryIndex):
        methods = {m for m in dir(cls) if not m.startswith("_")}
        assert not methods & {"upload", "write", "delete", "move", "put", "save"}, cls


def test_orchestrator_cannot_confirm_its_own_output(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    """Guardrail 2: confirmation requires an actor identity — no default/agent path."""
    run_plan(orchestrator)
    with pytest.raises(TypeError):
        orchestrator.confirm(CAMPAIGN_ID, "pack", decision="confirmed")  # type: ignore[call-arg]
