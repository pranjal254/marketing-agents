"""Tasks 1, 2, 4 — normalization across entry points, brief-schema validation with
missing-field codes, BC/F&O split-or-flag, compliance ceiling."""

from __future__ import annotations

from shiftai_shared.business_capability import DecisionAgentConfig

from campaign_identification.intake import merge_gap_answers, normalize_request
from campaign_identification.rules import check_bc_fo, touches_compliance
from campaign_identification.validation import validate_request


def test_normalize_form_aliases_and_lists() -> None:
    raw = {
        "Responder": "a@x.com",
        "Business Unit": "Technology",
        "Industry": "FinServ",
        "Segment": "Type 3",
        "Offer/Topic": "AI readiness",
        "channels": "email; linkedin",
        "Start": "2026-10-01",
        "budget": "Yes",
    }
    request = normalize_request(raw, "form", source_ref="form:row-9")
    assert request.requester == "a@x.com"
    assert request.vertical == "financial_services"
    assert request.target_segment == "type_3"
    assert request.channels == ["email", "linkedin"]
    assert request.budget_flag is True
    assert request.source_refs == ["form:row-9"]
    assert request.source == "form"


def test_normalize_never_invents_values() -> None:
    request = normalize_request({"topic": "x"}, "plan")
    assert request.objective is None
    assert request.owner is None
    assert request.budget_flag is None


def test_validation_enumerates_missing_with_codes(config: DecisionAgentConfig) -> None:
    request = normalize_request({"topic": "AI", "vertical": "manufacturing"}, "form")
    result = validate_request(request, config.intake_schema)
    assert not result.complete
    codes = {m.code for m in result.missing}
    assert "missing_objective" in codes
    assert "missing_business_unit" in codes
    assert 0 < result.completeness_score < 1


def test_validation_flags_ambiguous_select(config: DecisionAgentConfig) -> None:
    raw = {
        "objective": "o",
        "business_unit": "Tech",
        "vertical": "retail",
        "target_segment": "type_3",
        "offer_topic": "t",
        "channels": "email",
        "timeline_start": "2026-01-01",
        "timeline_end": "2026-02-01",
        "owner": "x",
        "budget_flag": "yes",
        "requester": "r@x.com",
    }
    request = normalize_request(raw, "form")
    result = validate_request(request, config.intake_schema)
    assert any(m.code == "ambiguous_vertical" for m in result.missing)


def test_validation_complete_request(config: DecisionAgentConfig, complete_raw: dict) -> None:
    request = normalize_request(complete_raw, "form")
    result = validate_request(request, config.intake_schema)
    assert result.complete and result.completeness_score == 1.0


def test_bc_fo_mix_flagged_never_merged() -> None:
    request = normalize_request(
        {
            "topic": "One campaign covering Business Central and F&O migration",
            "products": ["BC", "F&O"],
        },
        "adhoc",
    )
    check = check_bc_fo(request)
    assert check.mixed
    assert len(check.split_proposal) == 2
    assert any("Business Central" in p for p in check.split_proposal)
    assert any("F&O" in p for p in check.split_proposal)


def test_bc_only_not_flagged(complete_raw: dict) -> None:
    request = normalize_request(complete_raw, "form")
    assert not check_bc_fo(request).mixed


def test_compliance_ceiling_detects_pricing_legal_partner() -> None:
    request = normalize_request(
        {"topic": "Special pricing promo with partner commitments", "context": "legal review"},
        "adhoc",
    )
    hits = touches_compliance(request)
    assert "pricing" in hits
    assert any("partner commit" in h for h in hits)


def test_merge_gap_answers_fills_only_answered_fields(complete_raw: dict) -> None:
    request = normalize_request({"topic": "AI", "requester": "a@x.com"}, "form")
    merged = merge_gap_answers(request, {"objective": "drive pipeline"})
    assert merged.objective == "drive pipeline"
    assert merged.request_id == request.request_id
    assert merged.owner is None  # unanswered stays unanswered
