"""AI-first intake flow: free-text extraction, hold-for-verification, the requester
revision loop (directives), release, and BU return-with-note."""

from __future__ import annotations

import json
from typing import Any

from shiftai_shared.llm import MockLLMProvider

from tests.conftest import CLASSIFY_OK, GAP_QUESTIONS, records_of

DESCRIPTION = (
    "Build cloud migration intent with financial services CFOs on LinkedIn and "
    "email nurture, anchored on our delivery experience"
)

EXTRACT_OK = json.dumps(
    {
        "fields": {
            "objective": {
                "value": "Build cloud migration intent with financial services CFOs",
                "quote": "Build cloud migration intent with financial services CFOs",
            },
            "vertical": {"value": "Financial Services", "quote": "financial services CFOs"},
            "offer_topic": {
                "value": "Cloud migration for financial services",
                "quote": "cloud migration intent",
            },
            "channels": {"value": ["linkedin", "email"], "quote": "on LinkedIn and email nurture"},
            "business_unit": {"value": "Technology", "quote": "our delivery experience"},
            "target_segment": {"value": "type_3", "quote": "SHOULD BE IGNORED"},
        }
    }
)

REVISE_OK = json.dumps(
    {
        "objective": "Executive outcome first: cloud migration pipeline from FinServ CFOs",
        "offer_topic": "Cloud migration for financial services",
        "rationale": "led with the executive outcome per directive",
    }
)


def scripted_intake_provider() -> MockLLMProvider:
    return MockLLMProvider(
        default=CLASSIFY_OK,
        script=[
            (lambda u: "Extract campaign brief fields" in u, EXTRACT_OK),
            (lambda u: "Draft one specific, targeted question" in u, GAP_QUESTIONS),
            (lambda u: "wants their campaign brief fields revised" in u, REVISE_OK),
        ],
        model_name="mock-sonnet",
    )


def _describe(harness: dict[str, Any]) -> Any:
    return harness["agent"].process_request(
        {
            "requester": "rishi@levelshift.com",
            "owner": "rishi@levelshift.com",
            "free_text_context": DESCRIPTION,
        },
        "adhoc",
        hold_for_verification=True,
    )


def _fill_answers() -> dict[str, str]:
    return {
        "target_segment": "type_3",
        "budget_flag": "yes",
        "timeline_start": "2026-10-01",
        "timeline_end": "2026-11-15",
    }


def test_extraction_fills_only_stated_fields(harness: dict[str, Any]) -> None:
    harness["provider"] = harness["deps"].provider = scripted_intake_provider()
    harness["agent"].deps.provider = harness["deps"].provider
    outcome = _describe(harness)
    # extracted from the description, never routed anywhere yet
    assert outcome.status == "awaiting_input"
    case = harness["deps"].store.get("case", outcome.case_id)
    assert case is not None
    request = case.value["request"]
    assert request["vertical"] == "financial_services"
    assert request["channels"] == ["linkedin", "email"]
    assert "objective" in request["derived_fields"]
    # never-extracted fields stay with the human even when the model returns them
    assert request["target_segment"] is None
    assert request["budget_flag"] is None
    # gap questions cover exactly the human-only leftovers
    fields = {q.field for q in outcome.gap_request.questions}
    assert "target_segment" in fields and "budget_flag" in fields
    assert "objective" not in fields
    # the extraction call is telemetry-visible with model usage
    tools = records_of(harness["sink"], "tool_execution", outcome.case_id)
    assert any(t["gen_ai.tool.name"] == "layer1.extract_fields" for t in tools)


def test_revise_directive_rewrites_objective_in_awaiting_input(harness: dict[str, Any]) -> None:
    harness["agent"].deps.provider = scripted_intake_provider()
    outcome = _describe(harness)
    revised = harness["agent"].revise_brief(
        outcome.case_id,
        directive="lead with the executive outcome",
        aspects=["Executive angle"],
        actor_id="rishi@levelshift.com",
    )
    assert revised.status == "awaiting_input"
    case = harness["deps"].store.get("case", outcome.case_id)
    assert case is not None
    assert case.value["request"]["objective"].startswith("Executive outcome first")
    assert "revised per requester directive" in case.value["request"]["derived_fields"]["objective"]
    gates = records_of(harness["sink"], "human_gate", outcome.case_id)
    assert any(g["shiftai.learn.reason_code"] == "revision_directive" for g in gates)


def test_answers_with_release_route_to_approval(harness: dict[str, Any]) -> None:
    harness["agent"].deps.provider = scripted_intake_provider()
    outcome = _describe(harness)
    final = harness["agent"].submit_gap_answers(
        outcome.case_id, _fill_answers(), actor_id="rishi@levelshift.com", release_after=True
    )
    assert final.status == "awaiting_approval"
    assert final.brief is not None and final.doc_ref
    # exactly one routing action, after the requester's verification human_gate
    actions = records_of(harness["sink"], "action_taken", outcome.case_id)
    assert len(actions) == 1
    gates = records_of(harness["sink"], "human_gate", outcome.case_id)
    assert any(g["shiftai.learn.human_action"] == "verified_and_released" for g in gates)


def test_hold_without_release_stays_in_draft_review(harness: dict[str, Any]) -> None:
    harness["agent"].deps.provider = scripted_intake_provider()
    outcome = _describe(harness)
    held = harness["agent"].submit_gap_answers(
        outcome.case_id, _fill_answers(), actor_id="rishi@levelshift.com"
    )
    assert held.status == "draft_review"
    assert held.brief is not None  # drafted + written to workspace
    assert records_of(harness["sink"], "action_taken", outcome.case_id) == []  # not routed

    # directive on the held draft bumps the brief version and re-uploads
    revised = harness["agent"].revise_brief(
        outcome.case_id, directive="tighter objective", actor_id="rishi@levelshift.com"
    )
    assert revised.status == "draft_review"
    assert revised.brief is not None and revised.brief.version == 2
    assert harness["workspace"].uploads == 2

    released = harness["agent"].release_brief(outcome.case_id, actor_id="rishi@levelshift.com")
    assert released.status == "awaiting_approval"


def test_bu_return_sends_brief_back_to_requester(harness: dict[str, Any]) -> None:
    harness["agent"].deps.provider = scripted_intake_provider()
    outcome = _describe(harness)
    harness["agent"].submit_gap_answers(
        outcome.case_id, _fill_answers(), actor_id="rishi@levelshift.com", release_after=True
    )
    returned = harness["agent"].record_human_decision(
        outcome.case_id,
        "returned",
        actor_role="bu-campaign-lead",
        actor_id="marcus@levelshift.com",
        notes="Sharpen the offer before I approve",
    )
    assert returned.status == "draft_review"
    case = harness["deps"].store.get("case", outcome.case_id)
    assert case is not None
    assert case.value["returned_note"] == "Sharpen the offer before I approve"
    # not terminal: no case_resolved emitted
    assert records_of(harness["sink"], "case_resolved", outcome.case_id) == []
    # requester revises and re-releases; BU approves this time
    harness["agent"].revise_brief(
        outcome.case_id, directive="Sharpen the offer", actor_id="rishi@levelshift.com"
    )
    harness["agent"].release_brief(outcome.case_id, actor_id="rishi@levelshift.com")
    final = harness["agent"].record_human_decision(
        outcome.case_id,
        "approved",
        actor_role="bu-campaign-lead",
        actor_id="marcus@levelshift.com",
    )
    assert final.status == "approved"
    resolved = records_of(harness["sink"], "case_resolved", outcome.case_id)
    assert resolved and resolved[0]["shiftai.outcome"] == "success"


def test_extraction_failure_falls_back_to_gap_flow(harness: dict[str, Any]) -> None:
    # default mock returns CLASSIFY_OK for extraction too → unparsable as extraction
    # payload (no "fields") → request unchanged → normal gap flow
    outcome = harness["agent"].process_request(
        {"requester": "a@x.com", "free_text_context": "vague words"},
        "adhoc",
        hold_for_verification=True,
    )
    assert outcome.status == "awaiting_input"
    assert outcome.gap_request is not None
