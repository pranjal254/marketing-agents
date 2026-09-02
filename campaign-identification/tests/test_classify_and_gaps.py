"""Task 5 + 6 — L3 classification (prompt structure, parsing, abstention, the
never-invent enforcement) and targeted gap drafting with deterministic fallback."""

from __future__ import annotations

import json

from shiftai_shared.business_capability import DecisionAgentConfig
from shiftai_shared.llm import MockLLMProvider

from campaign_identification.classify import (
    build_case_data,
    derive_priority,
    load_system_prompt,
    run_classification,
    system_blocks,
)
from campaign_identification.gaps import draft_gap_request
from campaign_identification.intake import normalize_request
from campaign_identification.models import BcFoCheck, MissingField, ValidationResult
from campaign_identification.rules import check_bc_fo
from campaign_identification.validation import validate_request
from tests.conftest import CLASSIFY_OK


def _inputs(config: DecisionAgentConfig, raw: dict) -> tuple:
    request = normalize_request(raw, "form")
    validation = validate_request(request, config.intake_schema)
    case_data = build_case_data(request, validation, [], check_bc_fo(request), "high")
    return request, case_data


def test_prompt_uses_spec_system_prompt_and_case_data_guard(
    config: DecisionAgentConfig, complete_raw: dict
) -> None:
    request, case_data = _inputs(config, complete_raw)
    provider = MockLLMProvider(default=CLASSIFY_OK)
    output, _response, user_prompt = run_classification(provider, config, case_data, request)
    call = provider.calls[0]
    system_texts = [b["text"] for b in call["system"]]  # type: ignore[index]
    # exact spec prompt, loaded from the versioned file, as a cached block
    assert system_texts[0] == load_system_prompt()
    assert all(b["cache"] for b in call["system"])  # type: ignore[index]
    # all request free text rides inside <case_data>
    assert "<case_data>" in user_prompt
    free_text = complete_raw["free_text_context"]
    inside = user_prompt.split("<case_data>")[-1].split("</case_data>")[0]
    assert free_text in inside
    assert output.action_class == "route_for_approval"


def test_action_class_outside_taxonomy_becomes_abstention(
    config: DecisionAgentConfig, complete_raw: dict
) -> None:
    request, case_data = _inputs(config, complete_raw)
    bad = json.dumps({"action_class": "made_up_class", "confidence": 0.9, "rationale": "x"})
    provider = MockLLMProvider(default=bad)
    output, _, _ = run_classification(provider, config, case_data, request)
    assert output.action_class is None
    assert output.confidence == 0.0
    assert len(provider.calls) == 2  # one retry, then abstain


def test_unparsable_output_retries_once_then_abstains(
    config: DecisionAgentConfig, complete_raw: dict
) -> None:
    request, case_data = _inputs(config, complete_raw)
    provider = MockLLMProvider(default="I think this is a great campaign!")
    output, _, _ = run_classification(provider, config, case_data, request)
    assert output.action_class is None
    assert len(provider.calls) == 2


def test_never_invent_drops_fields_absent_from_request(
    config: DecisionAgentConfig, complete_raw: dict
) -> None:
    raw = dict(complete_raw)
    del raw["owner"]
    request, case_data = _inputs(config, raw)
    invented = json.loads(CLASSIFY_OK)
    invented["normalized_fields"] = {"owner": "made.up@x.com", "offer_topic": "ERP modernization"}
    provider = MockLLMProvider(default=json.dumps(invented))
    output, _, _ = run_classification(provider, config, case_data, request)
    assert "owner" not in output.normalized_fields  # request had none → cannot be invented
    assert output.normalized_fields.get("offer_topic") == "ERP modernization"


def test_derive_priority() -> None:
    high = normalize_request({"vertical": "financial_services"}, "plan")
    assert derive_priority(high, plan_linked=True) == "high"
    low = normalize_request({"vertical": "unknown"}, "adhoc")
    assert derive_priority(low, plan_linked=False) == "low"


def test_gap_request_targets_each_missing_field(config: DecisionAgentConfig) -> None:
    request = normalize_request({"requester": "a@x.com", "topic": "AI"}, "form")
    missing = [
        MissingField(field="objective", code="missing_objective", kind="missing"),
        MissingField(field="owner", code="missing_owner", kind="missing"),
    ]
    provider = MockLLMProvider(
        default=json.dumps(
            {"questions": [{"field": "objective", "question": "What outcome do you want?"}]}
        )
    )
    gap, response = draft_gap_request(
        provider, system_blocks(config), request, missing, round_number=1
    )
    fields = {q.field for q in gap.questions}
    assert fields == {"objective", "owner"}  # LLM question + deterministic fallback
    assert gap.sent_to == "a@x.com"
    assert gap.round == 1
    assert response is not None


def test_gap_request_full_fallback_when_llm_fails(config: DecisionAgentConfig) -> None:
    request = normalize_request({"requester": "a@x.com"}, "form")
    missing = [MissingField(field="objective", code="missing_objective", kind="missing")]

    class ExplodingProvider:
        def complete(self, **kwargs: object) -> object:
            raise RuntimeError("provider down")

    gap, response = draft_gap_request(
        ExplodingProvider(),
        system_blocks(config),
        request,
        missing,
        round_number=1,  # type: ignore[arg-type]
    )
    assert response is None
    assert len(gap.questions) == 1
    assert "objective" in gap.questions[0].question


def test_validation_result_used_not_llm(config: DecisionAgentConfig, incomplete_raw: dict) -> None:
    request = normalize_request(incomplete_raw, "form")
    validation = validate_request(request, config.intake_schema)
    assert isinstance(validation, ValidationResult)
    assert not validation.complete
    assert isinstance(check_bc_fo(request), BcFoCheck)
