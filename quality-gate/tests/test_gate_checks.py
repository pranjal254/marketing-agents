"""Gate pass behavior: deterministic + contextual checks, fail-closed semantics,
hash chain-of-custody, delta re-check, and the flags-never-edits guarantee."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from conftest import (
    CAMPAIGN_ID,
    CLEAN_TEXT,
    FOLDER,
    INTERNAL_TEXT,
    RecordingSignals,
    ScriptedGateProvider,
    events_of,
    seed_manifest,
    sha,
)
from shiftai_shared.telemetry import InMemorySink

from c2c_quality_gate import persistence as db
from c2c_quality_gate.models import CONTEXTUAL_RULES
from c2c_quality_gate.orchestration import (
    GateStateError,
    QualityGateAgent,
)


def test_clean_package_passes_and_routes(agent: QualityGateAgent, workspace, signals) -> None:
    seed_manifest(agent.deps.store, workspace)
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.status == "in_review"
    assert outcome.failed_asset_ids == []
    assert all(r.verdict == "pass" for r in outcome.reports)
    assert not signals.calls  # nothing failed → nothing signaled
    tasks = db.load_tasks(agent.deps.store, CAMPAIGN_ID)
    assert any(t.scope == "package" for t in tasks)


def test_no_manifest_is_a_gate_state_error(agent: QualityGateAgent) -> None:
    with pytest.raises(GateStateError, match="manifest"):
        agent.run_gate(CAMPAIGN_ID)


def test_banned_language_blocks_and_returns_to_collaboration(
    agent: QualityGateAgent, workspace, signals: RecordingSignals, sink: InMemorySink
) -> None:
    bad = CLEAN_TEXT.replace(
        "measurable outcomes", "measurable outcomes — act now before it's too late"
    )
    seed_manifest(agent.deps.store, workspace, {
        "flagship_blog": bad.encode(),
        "linkedin_posts": CLEAN_TEXT.encode(),
        "battle_card": INTERNAL_TEXT.encode(),
    })
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.status == "gate_failed"
    assert outcome.failed_asset_ids == ["flagship_blog"]
    report = db.load_report(agent.deps.store, CAMPAIGN_ID, "flagship_blog")
    assert report is not None
    hits = [f for f in report.findings if f.rule_id == "urgency_fear"]
    assert hits and hits[0].severity == "blocking" and hits[0].location.startswith("line")
    # failed assets go back to the Collaboration Agent WITH findings
    kinds = [c[0] for c in signals.calls]
    assert kinds == ["assets_failed"]
    _, payload = signals.calls[0][1]
    assert any("urgency_fear" in line for line in payload["flagship_blog"])
    assert events_of(sink, "case_escalated")


def test_unresolved_marker_is_unsourced_claim(agent: QualityGateAgent, workspace) -> None:
    text = CLEAN_TEXT + "\nCompetitors take 3x longer to deploy [c-9]."
    seed_manifest(agent.deps.store, workspace, {"flagship_blog": text.encode()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    report = outcome.reports[0]
    assert report.verdict == "fail"
    assert any(
        f.rule_id == "unsourced_claim" and f.severity == "blocking" and "[c-9]" in f.quote
        for f in report.findings
    )


def test_aeo_named_mention_required_on_faq_assets(
    agent: QualityGateAgent, workspace
) -> None:
    faq = "What is on-demand delivery? A scoped model billed for outcomes [c-1]."
    seed_manifest(agent.deps.store, workspace, {"faq_service_page": faq.encode()})
    # the faq asset has no staged draft — its [c-1] is also unresolved
    outcome = agent.run_gate(CAMPAIGN_ID)
    rules = {f.rule_id for f in outcome.reports[0].findings}
    assert "aeo_named_mention" in rules and "unsourced_claim" in rules


def test_hash_mismatch_fails_closed_and_pages_aicoe(
    agent: QualityGateAgent, workspace, sink: InMemorySink
) -> None:
    seed_manifest(agent.deps.store, workspace, tamper_hash_for="flagship_blog")
    outcome = agent.run_gate(CAMPAIGN_ID)
    report = next(r for r in outcome.reports if r.asset_id == "flagship_blog")
    assert report.verdict == "fail"
    assert any(f.rule_id == "hash_mismatch" for f in report.findings)
    routed = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "hash_mismatch"
    ]
    assert routed and routed[0]["shiftai.escalation.routed_to"] == "aicoe-queue"


def test_incomplete_contextual_checks_fail_closed(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    # the model "forgets" to complete one contextual rule — never defaults to pass
    completed = sorted(set(CONTEXTUAL_RULES) - {"claim_sourcing"})
    provider.reply = json.dumps(
        {"findings": [], "checks_completed": completed, "confidence": 0.9}
    )
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": CLEAN_TEXT.encode()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    report = outcome.reports[0]
    assert report.verdict == "fail"
    assert not report.checks_complete
    assert "claim_sourcing" in report.incomplete_reason


def test_unparsable_contextual_reply_keeps_deterministic_findings(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    provider.reply = "I could not produce JSON."
    bad = CLEAN_TEXT + "\nWe seamlessly leverage synergies."
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": bad.encode()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    report = outcome.reports[0]
    assert report.verdict == "fail" and not report.checks_complete
    # deterministic pass stands even when the contextual pass degrades (partial report)
    assert any(f.source == "deterministic" for f in report.findings)


def test_model_invented_rules_are_dropped_and_severity_is_policy(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    provider.reply = json.dumps({
        "findings": [
            {"rule_id": "my_own_rule", "severity": "blocking", "quote": "x"},
            {"rule_id": "brand_voice", "severity": "blocking",
             "location": "line 2", "quote": "LevelShift helps manufacturers",
             "reasoning": "tone drift", "remediation": "align voice"},
        ],
        "checks_completed": sorted(CONTEXTUAL_RULES),
        "confidence": 0.8,
    })
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": CLEAN_TEXT.encode()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    report = outcome.reports[0]
    rules = {f.rule_id for f in report.findings}
    assert "my_own_rule" not in rules  # invented rule never enters the report
    voice = next(f for f in report.findings if f.rule_id == "brand_voice")
    assert voice.severity == "advisory"  # policy severity, not the model's
    assert voice.quote_verified  # verbatim quote check
    assert report.verdict == "pass"  # advisory only


def test_unverifiable_quote_is_kept_but_marked(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    provider.reply = json.dumps({
        "findings": [{"rule_id": "tone_urgency_fear", "severity": "blocking",
                      "location": "line 1", "quote": "not actually in the text",
                      "reasoning": "r", "remediation": "m"}],
        "checks_completed": sorted(CONTEXTUAL_RULES),
        "confidence": 0.8,
    })
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": CLEAN_TEXT.encode()})
    report = agent.run_gate(CAMPAIGN_ID).reports[0]
    finding = next(f for f in report.findings if f.rule_id == "tone_urgency_fear")
    assert not finding.quote_verified
    assert report.verdict == "fail"  # fail-closed: over-flagging is the safe direction


def test_rerun_reuses_reports_for_unchanged_assets(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    seed_manifest(agent.deps.store, workspace)
    first = agent.run_gate(CAMPAIGN_ID)
    calls_after_first = len(provider.calls)
    second = agent.run_gate(CAMPAIGN_ID)
    assert first.reused_reports == 0
    assert second.reused_reports == len(second.reports)
    assert len(provider.calls) == calls_after_first  # no new LLM spend


def test_gate_never_edits_content(agent: QualityGateAgent, workspace) -> None:
    manifest = seed_manifest(agent.deps.store, workspace)
    before = {a.asset_id: Path(a.snapshot_ref).read_bytes() for a in manifest.assets}
    agent.run_gate(CAMPAIGN_ID)
    after = {a.asset_id: Path(a.snapshot_ref).read_bytes() for a in manifest.assets}
    assert before == after
    for asset in manifest.assets:
        assert sha(after[asset.asset_id]) == asset.sha256


def test_paused_control_plane_blocks_the_gate(
    agent: QualityGateAgent, workspace
) -> None:
    seed_manifest(agent.deps.store, workspace)
    agent.deps.kill_switch.pause(agent.deps.config.agent_id, "governance hold")
    with pytest.raises(GateStateError, match="paused"):
        agent.run_gate(CAMPAIGN_ID)


def test_signal_failure_never_hides_the_verdict(
    agent: QualityGateAgent, workspace, signals: RecordingSignals, sink: InMemorySink
) -> None:
    signals.raise_on.add("assets_failed")
    bad = CLEAN_TEXT + "\nDon't miss this limited time offer."
    seed_manifest(agent.deps.store, workspace, {"flagship_blog": bad.encode()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.status == "gate_failed"  # verdict recorded despite signal failure
    tool_failures = [
        r for r in events_of(sink, "case_escalated")
        if r.get("shiftai.learn.reason_code") == "tool_failure"
    ]
    assert tool_failures


def test_docx_snapshots_are_read_as_text(agent: QualityGateAgent, workspace) -> None:
    import io

    from docx import Document

    buf = io.BytesIO()
    document = Document()
    for line in CLEAN_TEXT.splitlines():
        document.add_paragraph(line)
    document.save(buf)
    seed_manifest(agent.deps.store, workspace, {"flagship_blog": buf.getvalue()})
    outcome = agent.run_gate(CAMPAIGN_ID)
    assert outcome.reports[0].verdict == "pass"  # markers resolved from real docx text


def test_workspace_layout_untouched_outside_final(
    agent: QualityGateAgent, workspace: LocalCampaignWorkspace
) -> None:
    seed_manifest(agent.deps.store, workspace)
    agent.run_gate(CAMPAIGN_ID)
    root = Path(workspace.root)
    files = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert all(f.startswith(f"{FOLDER}/confirmed/") for f in files)


def test_lineage_markers_resolve_like_claim_markers(
    agent: QualityGateAgent, workspace
) -> None:
    """Derivatives cite [cl-N] lineage ids from the flagship's verified claim
    inventory — the gate must resolve them, not flag them as unsourced. Only a
    lineage id the draft does NOT carry stays blocking."""
    text = (
        "Retailers report 42% faster planning cycles [c-1].\n"
        "DemandBlue ties the work to sourced outcomes [cl-1].\n"
        "An uncited outcome claim [cl-99]."
    )
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": text.encode()})
    report = agent.run_gate(CAMPAIGN_ID).reports[0]
    unsourced = [f for f in report.findings if f.rule_id == "unsourced_claim"]
    flagged = {f.quote for f in unsourced}
    assert any("[cl-99]" in q for q in flagged)  # unknown lineage id → blocking
    assert not any("[cl-1]" in q for q in flagged)  # known lineage id → resolved
    assert not any("[c-1]" in q for q in flagged)  # claim marker → resolved


def test_no_change_needed_pseudo_findings_are_dropped(
    agent: QualityGateAgent, provider: ScriptedGateProvider, workspace
) -> None:
    """The model sometimes lists a COMPLIANT claim as a 'finding' with reasoning
    'no change needed ... already includes a resolvable marker'. Such non-findings
    must never block the asset."""
    provider.reply = json.dumps({
        "findings": [
            {"rule_id": "claim_sourcing", "severity": "blocking", "location": "line 1",
             "quote": "Retailers report 42% faster planning cycles [c-1].",
             "reasoning": "No change needed because it already includes marker [c-1].",
             "remediation": "No change needed."},
            {"rule_id": "copilot_scope", "severity": "blocking", "location": "whole asset",
             "quote": "", "reasoning": "No change needed because Copilot is not mentioned.",
             "remediation": "None."},
        ],
        "checks_completed": sorted(CONTEXTUAL_RULES),
        "confidence": 0.9,
    })
    seed_manifest(agent.deps.store, workspace, {"linkedin_posts": CLEAN_TEXT.encode()})
    report = agent.run_gate(CAMPAIGN_ID).reports[0]
    assert report.verdict == "pass"  # both pseudo-findings dropped
    assert report.findings == []
