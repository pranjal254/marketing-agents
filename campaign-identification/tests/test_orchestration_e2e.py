"""End-to-end pipeline runs (all connectors mocked) — event sequences per
telemetry-standard.md §9; every emitted record is schema-validated at emit time."""

from __future__ import annotations

from typing import Any

from tests.conftest import events, records_of


def test_happy_path_full_lifecycle(harness: dict[str, Any], complete_raw: dict) -> None:
    agent = harness["agent"]
    sink = harness["sink"]
    outcome = agent.process_request(complete_raw, "form")
    assert outcome.status == "awaiting_approval"
    assert outcome.action_class == "route_for_approval"
    assert outcome.brief is not None and outcome.doc_ref
    seq = events(sink, outcome.case_id)
    assert seq[:3] == ["case_intake", "config_loaded", "policy_check"]
    assert "decision_made" in seq and "action_taken" in seq and "tool_execution" in seq

    # brief cannot advance without a human: nothing resolved yet
    assert "case_resolved" not in seq

    final = agent.record_human_decision(
        outcome.case_id, "approved", actor_role="bu-campaign-lead", actor_id="lead@x.com"
    )
    assert final.status == "approved"
    seq = events(sink, outcome.case_id)
    for required in (
        "case_intake",
        "config_loaded",
        "decision_made",
        "human_gate",
        "case_resolved",
        "run_summary",
    ):
        assert required in seq
    resolved = records_of(sink, "case_resolved", outcome.case_id)[0]
    assert resolved["shiftai.outcome"] == "success"
    assert resolved["shiftai.resolution.outcome_source"] == "human"
    # whole journey shares one trace id
    assert len({r["shiftai.trace.id"] for r in sink.records}) == 1


def test_l3_record_carries_genai_and_cost(harness: dict[str, Any], complete_raw: dict) -> None:
    agent = harness["agent"]
    outcome = agent.process_request(complete_raw, "form")
    decision = next(
        r
        for r in records_of(harness["sink"], "decision_made", outcome.case_id)
        if r.get("shiftai.decision.layer") == 3
    )
    assert decision["gen_ai.request.model"] == "claude-sonnet-5"
    assert decision["gen_ai.usage.input_tokens"] > 0
    assert decision["shiftai.prompt.template.id"] == "layer3-reasoning"
    assert decision["shiftai.prompt.template.version"] == "1.0.0"
    assert decision["shiftai.layer"] == "L3"


def test_incomplete_request_holds_awaiting_input(
    harness: dict[str, Any], incomplete_raw: dict
) -> None:
    agent = harness["agent"]
    outcome = agent.process_request(incomplete_raw, "form")
    assert outcome.status == "awaiting_input"
    assert outcome.gap_request is not None
    assert outcome.gap_request.questions  # targeted questions, not a generic bounce
    escalated = records_of(harness["sink"], "case_escalated", outcome.case_id)[0]
    assert escalated["shiftai.escalation.tier"] == 1
    assert escalated["shiftai.escalation.routed_to"] == "requester"
    assert escalated["shiftai.escalation.uncertainty_type"] == "data_ambiguity"
    assert harness["workspace"].uploads == 0  # nothing advanced downstream


def test_gap_answers_resume_and_route(harness: dict[str, Any], incomplete_raw: dict) -> None:
    agent = harness["agent"]
    first = agent.process_request(incomplete_raw, "form")
    answers = {
        "objective": "Grow FinServ pipeline",
        "business_unit": "Technology",
        "target_segment": "type_4",
        "channels": "events,email",
        "timeline_start": "2026-10-01",
        "timeline_end": "2026-11-01",
        "owner": "arjun.sales@levelshift.com",
        "budget_flag": "yes",
    }
    outcome = agent.submit_gap_answers(first.case_id, answers, actor_id="arjun.sales@x.com")
    assert outcome.status == "awaiting_approval"
    gates = records_of(harness["sink"], "human_gate", first.case_id)
    assert gates and gates[0]["shiftai.hitl.decision"] == "modified"
    # same case + trace across the gap round-trip
    assert outcome.case_id == first.case_id
    assert outcome.trace_id == first.trace_id


def test_bc_fo_mix_escalates_never_merges(harness: dict[str, Any], complete_raw: dict) -> None:
    raw = dict(complete_raw)
    raw["offer_topic"] = "Joint Business Central and F&O modernization story"
    raw["products"] = ["BC", "FO"]
    agent = harness["agent"]
    outcome = agent.process_request(raw, "form")
    assert outcome.status == "escalated"
    assert outcome.action_class == "flag_bc_fo_mix"
    record = records_of(harness["sink"], "case_escalated", outcome.case_id)[0]
    assert record["shiftai.escalation.tier"] == 2
    assert record["shiftai.learn.reason_code"] == "bc_fo_mix"
    assert harness["workspace"].uploads == 0


def test_fresh_duplicate_escalates_for_human_decision(
    harness: dict[str, Any], complete_raw: dict
) -> None:
    agent = harness["agent"]
    # First campaign approved → registered in the calendar
    first = agent.process_request(complete_raw, "form")
    agent.record_human_decision(
        first.case_id, "approved", actor_role="bu-campaign-lead", actor_id="lead@x.com"
    )
    # Same request again → fresh duplicate flag
    second = agent.process_request(complete_raw, "form")
    assert second.status == "escalated"
    assert second.action_class == "flag_duplicate"
    assert second.escalation_reason == "duplicate_disputed"


def test_compliance_ceiling_tier3(harness: dict[str, Any], complete_raw: dict) -> None:
    raw = dict(complete_raw)
    raw["free_text_context"] = "Includes special pricing and partner commitments"
    outcome = harness["agent"].process_request(raw, "form")
    assert outcome.status == "escalated"
    record = records_of(harness["sink"], "case_escalated", outcome.case_id)[0]
    assert record["shiftai.escalation.tier"] == 3


def test_rejection_resolves_cancelled(harness: dict[str, Any], complete_raw: dict) -> None:
    agent = harness["agent"]
    outcome = agent.process_request(complete_raw, "form")
    final = agent.record_human_decision(
        outcome.case_id, "rejected", actor_role="bu-campaign-lead", actor_id="lead@x.com"
    )
    assert final.status == "rejected"
    resolved = records_of(harness["sink"], "case_resolved", outcome.case_id)[0]
    assert resolved["shiftai.outcome"] == "cancelled"
    gate = records_of(harness["sink"], "human_gate", outcome.case_id)[0]
    assert gate["shiftai.learn.label"] == "false_positive"


def test_failure_persists_request_and_emits_error(
    harness: dict[str, Any], complete_raw: dict
) -> None:
    class ExplodingStore:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("store down")

    agent = harness["agent"]
    agent.deps.store = ExplodingStore()  # type: ignore[assignment]
    outcome = agent.process_request(complete_raw, "form")
    assert outcome.status == "failed"
    errors = records_of(harness["sink"], "error", outcome.case_id)
    assert errors and errors[0]["error.type"] == "RuntimeError"
    summary = records_of(harness["sink"], "run_summary", outcome.case_id)[0]
    assert summary["shiftai.outcome"] == "failure"
    assert summary["error.type"] == "RuntimeError"
