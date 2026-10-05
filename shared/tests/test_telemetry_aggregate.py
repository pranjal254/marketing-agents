"""Aggregation is only trustworthy if a merged snapshot equals a single pass
over the same records. These tests pin that property, plus the cursor rule that
keeps repeated refreshes from double-counting.
"""

from __future__ import annotations

from typing import Any

from shiftai_shared.telemetry.aggregate import (
    aggregate_records,
    empty_snapshot,
    merge_snapshots,
    record_is_after,
)


def _record(
    seq: int,
    *,
    stage: str = "drafting",
    ordinal: int = 4,
    agent: str = "content_repurposing",
    event: str = "decision_made",
    cost: float | None = None,
    duration: int | None = None,
    model: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "shiftai.schema.version": "2.0.0",
        "shiftai.tenant.id": "levelshift",
        "shiftai.agent.id": agent,
        "shiftai.agent.type": "orchestrator",
        "shiftai.process.name": "content-to-campaign",
        "shiftai.process.version": "1.0.0",
        "shiftai.stage.id": stage,
        "shiftai.stage.ordinal": ordinal,
        "deployment.environment.name": "dev",
        "shiftai.event.type": event,
        "shiftai.case.id": f"case_{seq % 3}",
        "shiftai.trace.id": f"trace_{seq % 2}",
        "shiftai.timestamp": f"2026-10-04T10:{seq:02d}:00Z",
        "bridge.seq": seq,
    }
    if cost is not None:
        record["shiftai.cost.amount"] = cost
    if duration is not None:
        record["shiftai.span.duration_ms"] = duration
    if model is not None:
        record["gen_ai.request.model"] = model
        record["gen_ai.response.model"] = model
        record["gen_ai.usage.input_tokens"] = 100
        record["gen_ai.usage.output_tokens"] = 20
    record.update(extra)
    return record


def _comparable(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Everything except the fields that legitimately differ between a single
    pass and a merge (when it ran, and how many refreshes produced it)."""
    return {k: v for k, v in snapshot.items() if k not in {"generated_at", "refresh_count"}}


STAGE_LABELS = {"drafting": "Drafting", "compliance": "Compliance"}


def test_totals_and_breakdowns_add_up() -> None:
    records = [
        _record(1, cost=0.10, duration=1000, model="claude-opus-5"),
        _record(2, cost=0.20, duration=3000, model="claude-opus-5"),
        _record(3, stage="compliance", ordinal=7, agent="quality_gate_approval",
                event="case_escalated", **{"shiftai.escalation.reason": "thin_intel",
                                           "shiftai.escalation.routed_to": "lead-queue"}),
    ]
    snapshot = aggregate_records(records, stage_labels=STAGE_LABELS)

    assert snapshot["totals"]["records"] == 3
    assert snapshot["totals"]["llm_calls"] == 2
    assert snapshot["totals"]["cost_usd"] == 0.3
    assert snapshot["totals"]["escalations"] == 1
    assert snapshot["totals"]["input_tokens"] == 200
    # Three records, three distinct case ids modulo 3; two distinct traces.
    assert snapshot["totals"]["cases"] == 3
    assert snapshot["totals"]["traces"] == 2

    stages = {row["stage_id"]: row for row in snapshot["by_stage"]}
    assert stages["drafting"]["records"] == 2
    assert stages["drafting"]["stage_label"] == "Drafting"
    assert stages["drafting"]["duration_ms_avg"] == 2000.0
    assert stages["drafting"]["duration_ms_max"] == 3000
    assert stages["compliance"]["escalations"] == 1
    # by_stage sorts by journey order, not by volume.
    assert [row["stage_id"] for row in snapshot["by_stage"]] == ["drafting", "compliance"]

    reasons = {row["reason"]: row for row in snapshot["by_escalation_reason"]}
    assert reasons["thin_intel"]["routed_to"] == "lead-queue"


def test_stage_reported_by_id_when_no_labels_injected() -> None:
    snapshot = aggregate_records([_record(1)])
    row = snapshot["by_stage"][0]
    assert row["stage_id"] == "drafting"
    assert "stage_label" not in row


def test_merge_of_two_halves_equals_one_pass() -> None:
    records = [
        _record(1, cost=0.10, duration=1000, model="claude-opus-5"),
        _record(2, cost=0.20, duration=3000, model="claude-opus-5"),
        _record(3, stage="compliance", ordinal=7, agent="quality_gate_approval",
                event="human_gate", **{"shiftai.hitl.decision": "approved",
                                       "shiftai.hitl.actor.role": "marketing-lead"}),
        _record(4, cost=0.05, duration=500, model="claude-sonnet-5"),
    ]
    one_pass = aggregate_records(records, stage_labels=STAGE_LABELS)

    first = aggregate_records(records[:2], stage_labels=STAGE_LABELS)
    second = aggregate_records(records[2:], stage_labels=STAGE_LABELS)
    merged = merge_snapshots(merge_snapshots(empty_snapshot(), first), second)

    assert _comparable(merged) == _comparable(one_pass)
    assert merged["refresh_count"] == 2
    assert one_pass["refresh_count"] == 1


def test_merge_is_order_independent_for_disjoint_batches() -> None:
    early = aggregate_records([_record(1, cost=0.1, duration=10)], stage_labels=STAGE_LABELS)
    late = aggregate_records(
        [_record(9, stage="compliance", ordinal=7, cost=0.2, duration=90)],
        stage_labels=STAGE_LABELS,
    )
    forwards = merge_snapshots(merge_snapshots(empty_snapshot(), early), late)
    backwards = merge_snapshots(merge_snapshots(empty_snapshot(), late), early)
    assert _comparable(forwards) == _comparable(backwards)


def test_window_widens_and_cursor_takes_the_high_water_mark() -> None:
    early = aggregate_records([_record(1)])
    late = aggregate_records([_record(9)])
    merged = merge_snapshots(early, late)
    assert merged["window"]["first_record_at"] == "2026-10-04T10:01:00Z"
    assert merged["window"]["last_record_at"] == "2026-10-04T10:09:00Z"
    assert merged["cursor"] == late["cursor"]
    # Merging an older batch afterwards must not drag the cursor backwards.
    assert merge_snapshots(merged, early)["cursor"] == late["cursor"]


def test_cursor_filters_records_already_counted() -> None:
    records = [_record(i) for i in range(1, 5)]
    first = aggregate_records(records[:2])
    remaining = [r for r in records if record_is_after(r, first["cursor"])]
    assert [r["bridge.seq"] for r in remaining] == [3, 4]

    merged = merge_snapshots(first, aggregate_records(remaining))
    assert merged["totals"]["records"] == 4
    # A refresh that re-reads the whole stream adds nothing the second time.
    nothing_new = [r for r in records if record_is_after(r, merged["cursor"])]
    assert nothing_new == []


def test_empty_batch_merges_cleanly() -> None:
    first = aggregate_records([_record(1, cost=0.5)])
    merged = merge_snapshots(first, aggregate_records([]))
    assert merged["totals"]["records"] == 1
    assert merged["totals"]["cost_usd"] == 0.5
    assert merged["cursor"] == first["cursor"]


def test_errors_counted_from_event_type_or_error_attribute() -> None:
    snapshot = aggregate_records(
        [
            _record(1, event="error"),
            _record(2, **{"error.type": "timeout"}),
            _record(3),
        ]
    )
    assert snapshot["totals"]["errors"] == 2


def test_run_totals_are_not_added_on_top_of_the_spans_they_restate() -> None:
    """STS reports a run's spend twice: per span, then again as a run total.
    Summing both reports roughly double the real figure."""
    spans = [
        _record(1, cost=0.10, model="claude-opus-5", **{"shiftai.cost.scope": "span_incremental"}),
        _record(2, cost=0.20, model="claude-opus-5", **{"shiftai.cost.scope": "span_incremental"}),
    ]
    summary = _record(
        3, event="run_summary", cost=0.30,
        **{"shiftai.cost.scope": "run_total", "shiftai.outcome": "success"},
    )
    snapshot = aggregate_records([*spans, summary])

    assert snapshot["totals"]["cost_usd"] == 0.3  # not 0.6
    assert snapshot["totals"]["records"] == 3  # the summary still counts as a record
    # And the restatement must not inflate any breakdown either.
    outcome = next(r for r in snapshot["by_outcome"] if r["outcome"] == "success")
    assert outcome["cost_usd"] == 0.0
    assert outcome["records"] == 1


def test_cost_with_no_scope_is_still_counted() -> None:
    """Only an explicit run_total is skipped; an unscoped cost is real spend."""
    snapshot = aggregate_records([_record(1, cost=0.25)])
    assert snapshot["totals"]["cost_usd"] == 0.25
