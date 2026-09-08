"""Routing module behavior: risk-scaled sequences, structural sequence integrity,
human-only approvals with identity + hash, locking, post-lock integrity,
calibration, and SLA sweeps."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from conftest import (
    CAMPAIGN_ID,
    FOLDER,
    RecordingSignals,
    approve_all_asset_tasks,
    events_of,
    seed_manifest,
)
from shiftai_shared.telemetry import InMemorySink

from c2c_quality_gate import persistence as db
from c2c_quality_gate.orchestration import (
    GateSequencingError,
    GateStateError,
    QualityGateAgent,
)


def _gated(agent: QualityGateAgent, workspace) -> None:
    seed_manifest(agent.deps.store, workspace)
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.status == "in_review"


def test_review_depth_scales_with_risk_class(agent: QualityGateAgent, workspace) -> None:
    _gated(agent, workspace)
    tasks = db.load_tasks(agent.deps.store, CAMPAIGN_ID)
    steps = {(t.asset_id, t.step) for t in tasks if t.scope == "asset"}
    assert ("flagship_blog", "grammar_qa") in steps  # public → full QA
    assert ("battle_card", "grammar_qa_light") in steps  # internal → lighter pass
    package = [t for t in tasks if t.scope == "package"]
    assert len(package) == 1 and package[0].step == "bu_lead_signoff"


def test_package_signoff_cannot_jump_the_queue(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    _gated(agent, workspace)
    package = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID) if t.scope == "package"
    )
    with pytest.raises(GateSequencingError, match="every asset review approved"):
        agent.record_review_outcome(
            CAMPAIGN_ID, package.task_id, decision="approved",
            actor_id="lead@x", actor_role="bu-campaign-lead",
        )
    violations = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "sequence_violation"
    ]
    assert violations


def test_identity_is_required_for_every_decision(
    agent: QualityGateAgent, workspace
) -> None:
    _gated(agent, workspace)
    task = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID) if t.scope == "asset"
    )
    with pytest.raises(GateSequencingError, match="identity"):
        agent.record_review_outcome(
            CAMPAIGN_ID, task.task_id, decision="approved",
            actor_id="   ", actor_role="grammar-quality-reviewer",
        )


def test_full_approval_locks_the_package(
    agent: QualityGateAgent, workspace, signals: RecordingSignals
) -> None:
    _gated(agent, workspace)
    approve_all_asset_tasks(agent)
    package = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID) if t.scope == "package"
    )
    agent.record_review_outcome(
        CAMPAIGN_ID, package.task_id, decision="approved",
        actor_id="lead@x", actor_role="bu-campaign-lead",
    )
    state = db.load_gate_state(agent.deps.store, CAMPAIGN_ID)
    assert state is not None and state.status == "approved_locked"
    assert len(state.locks) == 3
    for lock in state.locks:
        path = Path(lock.final_ref)  # locked IN PLACE — packaging's snapshot copy
        assert path.is_file()
        assert not os.access(path, os.W_OK)  # read-only lock
    # the approval chain: identity + hash per decision, package record included
    approvals = db.load_approvals(agent.deps.store, CAMPAIGN_ID)
    assert all(a.actor_id and a.sha256 for a in approvals)
    assert any(a.scope == "package" for a in approvals)
    assert ("package_approved", (CAMPAIGN_ID, 1)) in signals.calls
    # a locked package never re-enters the gate directly
    with pytest.raises(GateStateError, match="locked"):
        agent.run_gate(CAMPAIGN_ID)


def test_return_with_findings_reopens_packaging(
    agent: QualityGateAgent, workspace, signals: RecordingSignals
) -> None:
    _gated(agent, workspace)
    task = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID)
        if t.scope == "asset" and t.asset_id == "flagship_blog"
    )
    agent.record_review_outcome(
        CAMPAIGN_ID, task.task_id, decision="returned",
        actor_id="qa@x", actor_role="grammar-quality-reviewer",
        notes="Two subject-verb agreement errors in the intro.",
        disputed_rule_ids=["urgency_fear"],
    )
    state = db.load_gate_state(agent.deps.store, CAMPAIGN_ID)
    assert state is not None and state.status == "returned"
    returned = [c for c in signals.calls if c[0] == "package_returned"]
    assert returned
    _, (_campaign, asset_ids, notes, _actor) = returned[0]
    assert asset_ids == ["flagship_blog"]
    assert notes == "Two subject-verb agreement errors in the intro."  # verbatim
    # the dispute became a labeled false-positive calibration example
    calibration = db.load_calibration(agent.deps.store, CAMPAIGN_ID)
    assert [(e.kind, e.rule_id) for e in calibration] == [("false_positive", "urgency_fear")]


def test_package_return_must_name_assets(agent: QualityGateAgent, workspace) -> None:
    _gated(agent, workspace)
    approve_all_asset_tasks(agent)
    package = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID) if t.scope == "package"
    )
    with pytest.raises(GateStateError, match="name the assets"):
        agent.record_review_outcome(
            CAMPAIGN_ID, package.task_id, decision="returned",
            actor_id="lead@x", actor_role="bu-campaign-lead", notes="rework the emails",
        )


def test_post_lock_modification_invalidates_the_package(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    _gated(agent, workspace)
    approve_all_asset_tasks(agent)
    package = next(
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID) if t.scope == "package"
    )
    agent.record_review_outcome(
        CAMPAIGN_ID, package.task_id, decision="approved",
        actor_id="lead@x", actor_role="bu-campaign-lead",
    )
    state = db.load_gate_state(agent.deps.store, CAMPAIGN_ID)
    assert state is not None
    victim = Path(state.locks[0].final_ref)
    victim.chmod(0o644)
    victim.write_bytes(b"tampered after lock")
    violated = agent.verify_locks(CAMPAIGN_ID)
    assert violated == [state.locks[0].asset_id]
    refreshed = db.load_gate_state(agent.deps.store, CAMPAIGN_ID)
    assert refreshed is not None and refreshed.status == "invalidated"
    alarms = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "post_lock_modification"
    ]
    assert alarms and alarms[0]["shiftai.escalation.tier"] == 3


def test_false_negative_recording_alerts_immediately(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    _gated(agent, workspace)
    event = agent.record_false_negative(
        CAMPAIGN_ID, "flagship_blog", rule_id="tone_urgency_fear",
        reviewer_id="qa@x", reviewer_role="grammar-quality-reviewer",
        notes="hype framing the gate missed",
    )
    assert event.kind == "false_negative"
    alarms = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "calibration_alert"
    ]
    assert alarms


def test_sweep_reminders_are_monotonic_then_escalate(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    _gated(agent, workspace)
    tasks = db.load_tasks(agent.deps.store, CAMPAIGN_ID)
    created = date.fromisoformat(tasks[0].created_at[:10])
    # halfway through the SLA → first reminder, once
    halfway = date.fromordinal(created.toordinal() + 1)
    first = agent.sweep(CAMPAIGN_ID, today=halfway)
    again = agent.sweep(CAMPAIGN_ID, today=halfway)
    assert first.reminders_sent > 0
    assert again.reminders_sent == 0  # monotonic
    # far past due → escalation, once
    overdue = date.fromordinal(created.toordinal() + 30)
    escalated = agent.sweep(CAMPAIGN_ID, today=overdue)
    assert escalated.reviews_escalated > 0
    assert agent.sweep(CAMPAIGN_ID, today=overdue).reviews_escalated == 0
    overdue_events = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "review_overdue"
    ]
    assert overdue_events


def test_package_slip_escalates_to_bu_lead_once(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    _gated(agent, workspace)
    state = db.load_gate_state(agent.deps.store, CAMPAIGN_ID)
    assert state is not None
    started = date.fromisoformat(state.gate_started_at[:10])
    late = date.fromordinal(started.toordinal() + 30)
    outcome = agent.sweep(CAMPAIGN_ID, today=late)
    assert outcome.package_slips_escalated == 1
    assert agent.sweep(CAMPAIGN_ID, today=late).package_slips_escalated == 0
    slips = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "package_slip"
    ]
    assert slips and slips[0]["shiftai.escalation.routed_to"] == "bu-lead-queue"


def test_regate_after_rework_rebuilds_only_changed_sequences(
    agent: QualityGateAgent, workspace
) -> None:
    manifest = seed_manifest(agent.deps.store, workspace)
    agent.run_gate(CAMPAIGN_ID)
    before = {
        t.task_id for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID)
        if t.scope == "asset" and t.asset_id == "battle_card"
    }
    # flagship reworked → new snapshot bytes + hash, manifest v2 (Agent 2 re-package)
    new_text = "Retailers report 42% faster planning cycles [c-1].\nA reworked flagship."
    ref = workspace.upload(f"{FOLDER}/confirmed", "gate-test-flagship-blog-v2.docx",
                           new_text.encode())
    from conftest import sha as _sha

    assets = [
        a.model_copy(update={"snapshot_ref": ref, "sha256": _sha(new_text.encode()),
                             "version": 2})
        if a.asset_id == "flagship_blog" else a
        for a in manifest.assets
    ]
    v2 = manifest.model_copy(update={"version": 2, "assets": assets})
    agent.deps.store.put("package_manifest", CAMPAIGN_ID, v2.model_dump())
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.reused_reports == 2  # unchanged assets reused (delta re-check)
    after = {
        t.task_id for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID)
        if t.scope == "asset" and t.asset_id == "battle_card" and t.status != "cancelled"
    }
    assert before == after  # untouched asset kept its sequence
    flagship_tasks = [
        t for t in db.load_tasks(agent.deps.store, CAMPAIGN_ID)
        if t.asset_id == "flagship_blog"
    ]
    assert any(t.status == "cancelled" for t in flagship_tasks)
    assert any(t.status == "open" for t in flagship_tasks)
