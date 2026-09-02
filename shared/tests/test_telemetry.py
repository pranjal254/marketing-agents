"""Emitter validity, kit fixtures as regression anchor, append-only sink surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shiftai_shared.telemetry import (
    InMemorySink,
    JsonlSink,
    StsEmitter,
    TelemetrySink,
    TelemetryValidationError,
    load_sts_schema,
    rate_card_cost,
)
from shiftai_shared.telemetry.schema import find_schema_path


def make_emitter(sink: InMemorySink | None = None) -> tuple[StsEmitter, InMemorySink]:
    sink = sink or InMemorySink()
    emitter = StsEmitter(
        sink,
        tenant_id="levelshift-internal",
        agent_id="test_agent",
        agent_type="decision",
        config_version="0.1.0",
        environment="dev",
        risk_tier="medium",
        data_classification="confidential",
        process_name="test-process",
    )
    return emitter, sink


def test_core_12_present_and_valid() -> None:
    emitter, sink = make_emitter()
    record = emitter.emit("case_intake", case_id="case_1", trace_id="trace_1")
    for required in [
        "shiftai.schema.version",
        "shiftai.tenant.id",
        "shiftai.agent.id",
        "shiftai.agent.type",
        "shiftai.config.version",
        "deployment.environment.name",
        "shiftai.timestamp",
        "shiftai.event.type",
        "shiftai.case.id",
        "shiftai.trace.id",
        "shiftai.risk.tier",
        "shiftai.data.classification",
    ]:
        assert required in record
    assert sink.records[0]["shiftai.event.type"] == "case_intake"


def test_decision_made_requires_payload() -> None:
    emitter, _ = make_emitter()
    with pytest.raises(TelemetryValidationError):
        emitter.emit("decision_made", case_id="c", trace_id="t")


def test_decision_made_abstention_null_action_class() -> None:
    emitter, sink = make_emitter()
    emitter.emit(
        "decision_made",
        case_id="c",
        trace_id="t",
        **{
            "shiftai.decision.action_class": None,
            "shiftai.decision.confidence": 0.2,
            "shiftai.decision.layer": 3,
            "shiftai.layer": "L3",
        },
    )
    assert sink.records[0]["shiftai.decision.action_class"] is None


def test_llm_record_requires_tokens_and_template_version() -> None:
    emitter, _ = make_emitter()
    with pytest.raises(TelemetryValidationError):
        emitter.emit(
            "decision_made",
            case_id="c",
            trace_id="t",
            **{
                "shiftai.decision.action_class": "x",
                "shiftai.decision.confidence": 0.9,
                "shiftai.decision.layer": 3,
                "shiftai.layer": "L3",
                "gen_ai.request.model": "claude-sonnet-5",
            },
        )


def test_invalid_event_type_rejected() -> None:
    emitter, _ = make_emitter()
    with pytest.raises(TelemetryValidationError):
        emitter.emit("autonomy_promotion", case_id="c", trace_id="t")


def test_additive_attributes_allowed() -> None:
    emitter, sink = make_emitter()
    emitter.emit(
        "human_gate",
        case_id="c",
        trace_id="t",
        **{
            "shiftai.hitl.decision": "approved",
            "shiftai.hitl.actor.role": "reviewer",
            "shiftai.learn.reason_code": "some_code",
            "shiftai.learn.label": "correct",
            "shiftai.run.id": "run_x",
        },
    )
    assert sink.records[0]["shiftai.learn.reason_code"] == "some_code"


def test_kit_fixtures_all_validate() -> None:
    fixtures = find_schema_path().parent.parent / "telemetry" / "fixtures.json"
    records = json.loads(fixtures.read_text(encoding="utf-8"))["records"]
    emitter, _ = make_emitter()
    for record in records:
        emitter.validate(record)
    assert len(records) == 11


def test_sinks_are_append_only() -> None:
    for sink_cls in (TelemetrySink, InMemorySink, JsonlSink):
        exposed = {name for name in dir(sink_cls) if not name.startswith("_")}
        assert not any(("delete" in n or "update" in n or "remove" in n) for n in exposed)


def test_jsonl_sink_appends(tmp_path: Path) -> None:
    path = tmp_path / "telemetry.jsonl"
    sink = JsonlSink(str(path))
    emitter, _ = make_emitter()
    record = emitter.emit("case_intake", case_id="c", trace_id="t")
    sink.emit(record)
    sink.emit(record)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_rate_card_cost() -> None:
    assert rate_card_cost("claude-sonnet-5", 1_000_000, 0) == 3.0
    assert rate_card_cost("claude-sonnet-5", 0, 1_000_000) == 15.0
    # cached reads at 10% of the input rate
    assert rate_card_cost("claude-sonnet-5", 1_000_000, 0, cache_read_input_tokens=1_000_000) == 0.3
    assert rate_card_cost("unknown-model", 100, 100) is None


def test_schema_loads() -> None:
    schema = load_sts_schema()
    assert schema["title"].startswith("ShiftAI Telemetry Standard")
