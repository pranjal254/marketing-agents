"""End-to-end: planning pass → confirmation gate → confirmed assets → packaging →
rework re-open → re-package. Mock provider, in-memory stores — no live calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shiftai_shared.telemetry import InMemorySink

from c2c_campaign_box.orchestration import CampaignBoxOrchestrator, PlanGateError
from tests.conftest import CAMPAIGN_ID, PLAN_DATE, run_plan

REQUIRED_ASSETS = [
    "flagship_blog",
    "email_touchpoints",
    "linkedin_posts",
    "faq_service_page",
    "external_one_pager",
    "call_scripts",
]


def _events(sink: InMemorySink) -> list[str]:
    return [str(r["shiftai.event.type"]) for r in sink.records]


def _confirm_both(orchestrator: CampaignBoxOrchestrator) -> None:
    orchestrator.confirm(CAMPAIGN_ID, "pack", decision="confirmed",
                         actor_id="lead@levelshift.com")
    orchestrator.confirm(CAMPAIGN_ID, "plan", decision="confirmed",
                         actor_id="lead@levelshift.com")


def _register_all(orchestrator: CampaignBoxOrchestrator, version: int = 1) -> None:
    for asset_id in REQUIRED_ASSETS:
        orchestrator.register_confirmed_asset(
            CAMPAIGN_ID,
            asset_id,
            filename=f"erp-modernization-{asset_id.replace('_', '-')}-v{version}.docx",
            content=f"{asset_id} confirmed content v{version}".encode(),
            actor_id="reviewer@levelshift.com",
            claim_refs=["brief:offer_topic"],
        )


# ------------------------------------------------------------- planning pass


def test_planning_pass_end_to_end(orchestrator: CampaignBoxOrchestrator,
                                  sink: InMemorySink, repo_candidate_ref: str) -> None:
    outcome = run_plan(orchestrator)
    assert outcome.status == "awaiting_confirmation"
    assert outcome.escalation_reasons == []

    # Pack: grounded proof points; the fabricated claim is excluded into gaps.
    assert outcome.pack is not None
    assert len(outcome.pack.proof_points) == 3
    assert all(p.status == "verified" for p in outcome.pack.proof_points)
    assert any("42%" in g for g in outcome.pack.gaps)
    assert outcome.pack.intel_mode == "intel_library_only"

    # Checklist: model's adapt decision kept (candidate was evaluated); items the
    # model skipped stay create; search was performed.
    assert outcome.checklist is not None
    by_id = {i.asset_id: i for i in outcome.checklist.items}
    assert by_id["faq_service_page"].decision == "adapt"
    assert by_id["faq_service_page"].reuse_ref == repo_candidate_ref
    assert by_id["email_touchpoints"].decision == "create"
    assert outcome.checklist.search_performed

    # Outlines only for create/adapt; unverifiable planned claims stripped.
    flagship_outline = next(o for o in outcome.outlines if o.asset_id == "flagship_blog")
    assert flagship_outline.sections[0].planned_claims == ["brief:offer_topic"]

    # Deterministic plan: feasible, flagship-first.
    assert outcome.plan is not None and outcome.plan.feasible

    # Workspace materialized from the versioned template.
    assert outcome.workspace_root is not None
    root = Path(outcome.workspace_root)
    assert root.name == "2026-Q4-erp-modernization"
    assert (root / "brief").is_dir() and (root / "drafts").is_dir() and (root / "final").is_dir()
    assert outcome.pack_doc_ref and Path(outcome.pack_doc_ref).exists()
    assert outcome.tracker_ref and Path(outcome.tracker_ref).read_bytes().startswith(b"asset_id")

    events = _events(sink)
    for expected in ["case_intake", "config_loaded", "tool_execution", "policy_check",
                     "decision_made", "action_taken", "run_summary"]:
        assert expected in events
    assert events.count("decision_made") == 2  # pack + reuse/outlines
    summary = next(r for r in sink.records if r["shiftai.event.type"] == "run_summary")
    assert summary["shiftai.outcome"] == "success"


def test_unknown_campaign_fails_structured(orchestrator: CampaignBoxOrchestrator,
                                           sink: InMemorySink) -> None:
    outcome = orchestrator.plan_campaign("cmp_missing", plan_date=PLAN_DATE)
    assert outcome.status == "failed"
    assert outcome.escalation_reasons == ["brief_not_approved"]
    assert "error" in _events(sink)


def test_kill_switch_blocks_layer4(orchestrator: CampaignBoxOrchestrator) -> None:
    orchestrator.deps.kill_switch.pause(orchestrator.deps.config.agent_id, "governance drill")
    outcome = run_plan(orchestrator)
    assert outcome.status == "escalated"
    assert "control_pause" in outcome.escalation_reasons
    # No workspace side effects happened.
    assert outcome.workspace_root is None


def test_thin_intel_escalates_but_still_routes(orchestrator: CampaignBoxOrchestrator,
                                               provider, sink: InMemorySink) -> None:
    thin_pack = json.loads(provider.script[0][1])
    thin_pack["proof_points"] = [
        {"claim": "sourced", "source_ref": "brief:objective", "status": "verified"},
        {"claim": "unsourced A", "source_ref": "x", "status": "verified"},
        {"claim": "unsourced B", "source_ref": "y", "status": "verified"},
    ]
    provider.script[0] = (provider.script[0][0], json.dumps(thin_pack))
    outcome = run_plan(orchestrator)
    assert outcome.status == "awaiting_confirmation"  # human confirms anyway
    assert "thin_intel" in outcome.escalation_reasons
    escalation = next(r for r in sink.records if r["shiftai.event.type"] == "case_escalated")
    assert escalation["shiftai.learn.reason_code"] == "thin_intel"
    assert escalation["shiftai.escalation.reason"] == "policy_gap"  # schema enum mapping


# --------------------------------------------------------- confirmation gate


def test_confirmation_gate_two_keys_required(orchestrator: CampaignBoxOrchestrator) -> None:
    run_plan(orchestrator)
    first = orchestrator.confirm(CAMPAIGN_ID, "pack", decision="confirmed",
                                 actor_id="lead@levelshift.com")
    assert first.status == "awaiting_confirmation"
    second = orchestrator.confirm(CAMPAIGN_ID, "plan", decision="confirmed",
                                  actor_id="lead@levelshift.com")
    assert second.status == "in_production"
    assert second.checklist is not None
    assert all(i.status == "in_production" for i in second.checklist.items)


def test_pack_deltas_create_new_version(orchestrator: CampaignBoxOrchestrator) -> None:
    run_plan(orchestrator)
    outcome = orchestrator.confirm(
        CAMPAIGN_ID, "pack", decision="modified", actor_id="lead@levelshift.com",
        deltas={"value_proposition": "Sharper value proposition from the Lead"},
    )
    assert outcome.status == "awaiting_confirmation"
    assert outcome.pack is not None
    assert outcome.pack.version == 2
    assert outcome.pack.value_proposition == "Sharper value proposition from the Lead"


def test_modification_requires_deltas(orchestrator: CampaignBoxOrchestrator) -> None:
    run_plan(orchestrator)
    with pytest.raises(PlanGateError, match="delta"):
        orchestrator.confirm(CAMPAIGN_ID, "pack", decision="modified",
                             actor_id="lead@levelshift.com")


def test_asset_registration_needs_production_state(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    run_plan(orchestrator)
    with pytest.raises(PlanGateError):
        orchestrator.register_confirmed_asset(
            CAMPAIGN_ID, "flagship_blog", filename="x.docx", content=b"x",
            actor_id="reviewer@levelshift.com",
        )


# ---------------------------------------------------------------- packaging


def test_packaging_blocks_until_complete_then_succeeds(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    run_plan(orchestrator)
    _confirm_both(orchestrator)

    blocked = orchestrator.run_packaging(CAMPAIGN_ID)
    assert blocked.status == "packaging_blocked"
    assert blocked.report is not None
    assert set(blocked.report.diff.missing) == set(REQUIRED_ASSETS)
    assert "completeness_block" in blocked.escalation_reasons

    _register_all(orchestrator)
    packaged = orchestrator.run_packaging(CAMPAIGN_ID)
    assert packaged.status == "packaged_pending_compliance"
    assert packaged.manifest is not None
    manifest = packaged.manifest
    assert {a.asset_id for a in manifest.assets} == set(REQUIRED_ASSETS)
    assert all(len(a.sha256) == 64 for a in manifest.assets)
    assert all(Path(a.snapshot_ref).exists() for a in manifest.assets)
    assert manifest.claim_lineage_index["flagship_blog"] == ["brief:offer_topic"]
    # Snapshots are copies in final/ — sources in drafts/ untouched.
    assert all("final" in Path(a.snapshot_ref).parts for a in manifest.assets)
    assert all(Path(a.source_ref).exists() for a in manifest.assets)


def test_rework_reopen_and_repackage_rehashes(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    run_plan(orchestrator)
    _confirm_both(orchestrator)
    _register_all(orchestrator)
    first = orchestrator.run_packaging(CAMPAIGN_ID)
    assert first.manifest is not None

    reopened = orchestrator.reopen_assets(
        CAMPAIGN_ID, ["flagship_blog"], requesting_gate="quality-gate",
        actor_id="gate@levelshift.com",
    )
    assert reopened.status == "in_production"

    orchestrator.register_confirmed_asset(
        CAMPAIGN_ID, "flagship_blog",
        filename="erp-modernization-flagship-blog-v2.docx",
        content=b"flagship reworked content v2",
        actor_id="reviewer@levelshift.com",
    )
    second = orchestrator.run_packaging(CAMPAIGN_ID)
    assert second.status == "packaged_pending_compliance"
    assert second.manifest is not None and second.manifest.version == 2
    old = {a.asset_id: a.sha256 for a in first.manifest.assets}
    new = {a.asset_id: a.sha256 for a in second.manifest.assets}
    assert new["flagship_blog"] != old["flagship_blog"]  # re-hashed on re-entry
    assert new["call_scripts"] == old["call_scripts"]  # untouched assets stable


def test_post_packaging_edit_without_reopen_halts(
    orchestrator: CampaignBoxOrchestrator,
) -> None:
    run_plan(orchestrator)
    _confirm_both(orchestrator)
    _register_all(orchestrator)
    first = orchestrator.run_packaging(CAMPAIGN_ID)
    assert first.manifest is not None

    # Legitimate rework on one asset...
    orchestrator.reopen_assets(CAMPAIGN_ID, ["linkedin_posts"],
                               requesting_gate="quality-gate",
                               actor_id="gate@levelshift.com")
    orchestrator.register_confirmed_asset(
        CAMPAIGN_ID, "linkedin_posts",
        filename="erp-modernization-linkedin-posts-v2.docx",
        content=b"linkedin v2", actor_id="reviewer@levelshift.com",
    )
    # ...but someone also edits a packaged asset out-of-band (no re-open).
    flagship = next(a for a in first.manifest.assets if a.asset_id == "flagship_blog")
    Path(flagship.source_ref).write_bytes(b"tampered after packaging")

    halted = orchestrator.run_packaging(CAMPAIGN_ID)
    assert halted.status == "packaging_blocked"
    assert halted.escalation_reasons == ["hash_mismatch"]
