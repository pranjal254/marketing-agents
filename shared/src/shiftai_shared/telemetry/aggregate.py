"""Roll STS records up into a snapshot a dashboard can read without replaying
the stream. Engine mechanics: this module knows STS attribute names and nothing
about any particular business process.

Two properties drive the design.

*Mergeable.* Every figure is a counter, a sum, a maximum or a set union, so a
later snapshot merges exactly into an earlier one. Nothing here is an average or
a percentile at rest: averages are derived at read time from a sum and a count,
which stays exact across merges. That matters because the raw stream is a local
file and does not survive a restart, while snapshots live in the context store.
A refresh therefore aggregates only what it has and adds it to what is already
banked, rather than silently reporting a smaller total than last time.

*Cursored.* A snapshot records the last record it consumed. The next refresh
starts after that cursor, so repeated refreshes never double-count a record.

Cost needs one more rule. STS reports spend twice by design: once per span as
``span_incremental`` and once per run as ``run_total``, the latter restating
what its spans already reported. Only the incremental figures are summed here,
so a dashboard does not show roughly double the real spend.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

AGGREGATE_SCHEMA = "shiftai.telemetry.aggregate"
AGGREGATE_SCHEMA_VERSION = "1.0.0"

# STS attribute names this module reads.
A_TIMESTAMP = "shiftai.timestamp"
A_EVENT_TYPE = "shiftai.event.type"
A_CASE_ID = "shiftai.case.id"
A_TRACE_ID = "shiftai.trace.id"
A_AGENT_ID = "shiftai.agent.id"
A_AGENT_TYPE = "shiftai.agent.type"
A_STAGE_ID = "shiftai.stage.id"
A_STAGE_ORDINAL = "shiftai.stage.ordinal"
A_PROCESS_NAME = "shiftai.process.name"
A_PROCESS_VERSION = "shiftai.process.version"
A_SCHEMA_VERSION = "shiftai.schema.version"
A_ENVIRONMENT = "deployment.environment.name"
A_TENANT = "shiftai.tenant.id"
A_COST = "shiftai.cost.amount"
A_COST_SCOPE = "shiftai.cost.scope"
A_DURATION = "shiftai.span.duration_ms"
A_OUTCOME = "shiftai.outcome"
A_ESCALATION_REASON = "shiftai.escalation.reason"
A_ESCALATION_ROUTED_TO = "shiftai.escalation.routed_to"
A_HITL_DECISION = "shiftai.hitl.decision"
A_HITL_ROLE = "shiftai.hitl.actor.role"
A_ERROR_TYPE = "error.type"
A_REQUEST_MODEL = "gen_ai.request.model"
A_RESPONSE_MODEL = "gen_ai.response.model"
A_INPUT_TOKENS = "gen_ai.usage.input_tokens"
A_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
A_CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"

# A run summary restates the spend its spans already reported. Summing both
# reports roughly double the real figure, which on a cost dashboard is the
# worst kind of wrong: confident and plausible.
SCOPE_RUN_TOTAL = "run_total"

EVENT_ESCALATED = "case_escalated"
EVENT_HUMAN_GATE = "human_gate"
EVENT_ERROR = "error"

_METRIC_KEYS = (
    "records",
    "llm_calls",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cost_usd",
    "duration_ms_sum",
    "duration_ms_count",
    "escalations",
    "human_gates",
    "errors",
)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _zero_metrics() -> dict[str, float]:
    metrics: dict[str, float] = dict.fromkeys(_METRIC_KEYS, 0.0)
    metrics["duration_ms_max"] = 0.0
    return metrics


def _as_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _accumulate(bucket: dict[str, float], record: Mapping[str, Any]) -> None:
    bucket["records"] += 1
    if record.get(A_REQUEST_MODEL):
        bucket["llm_calls"] += 1
    bucket["input_tokens"] += _as_number(record.get(A_INPUT_TOKENS))
    bucket["output_tokens"] += _as_number(record.get(A_OUTPUT_TOKENS))
    bucket["cache_read_tokens"] += _as_number(record.get(A_CACHE_READ_TOKENS))
    # Incremental costs only; a run_total is a restatement, not new spend.
    if record.get(A_COST_SCOPE) != SCOPE_RUN_TOTAL:
        bucket["cost_usd"] += _as_number(record.get(A_COST))
    duration = record.get(A_DURATION)
    if isinstance(duration, int | float) and not isinstance(duration, bool):
        bucket["duration_ms_sum"] += float(duration)
        bucket["duration_ms_count"] += 1
        bucket["duration_ms_max"] = max(bucket["duration_ms_max"], float(duration))
    event_type = record.get(A_EVENT_TYPE)
    if event_type == EVENT_ESCALATED:
        bucket["escalations"] += 1
    if event_type == EVENT_HUMAN_GATE:
        bucket["human_gates"] += 1
    if event_type == EVENT_ERROR or record.get(A_ERROR_TYPE):
        bucket["errors"] += 1


def _merge_metrics(into: dict[str, float], other: Mapping[str, Any]) -> None:
    for key in _METRIC_KEYS:
        into[key] = _as_number(into.get(key)) + _as_number(other.get(key))
    into["duration_ms_max"] = max(
        _as_number(into.get("duration_ms_max")), _as_number(other.get("duration_ms_max"))
    )


def _round_metrics(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Read-time view: integers stay integers, money rounds, averages derive."""
    out: dict[str, Any] = {}
    for key in _METRIC_KEYS:
        value = _as_number(metrics.get(key))
        out[key] = round(value, 6) if key == "cost_usd" else int(value)
    out["duration_ms_max"] = int(_as_number(metrics.get("duration_ms_max")))
    count = out["duration_ms_count"]
    out["duration_ms_avg"] = (
        round(_as_number(metrics.get("duration_ms_sum")) / count, 1) if count else None
    )
    return out


class _Grouping:
    """One keyed breakdown (by stage, by agent and so on) plus its labels."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, float]] = {}
        self.labels: dict[str, dict[str, Any]] = {}

    def add(self, key: Any, record: Mapping[str, Any], **labels: Any) -> None:
        if key is None:
            return
        name = str(key)
        bucket = self.buckets.setdefault(name, _zero_metrics())
        _accumulate(bucket, record)
        known = self.labels.setdefault(name, {})
        for label, value in labels.items():
            if value is not None and label not in known:
                known[label] = value

    def load(self, rows: Iterable[Mapping[str, Any]], key_name: str) -> None:
        """Re-absorb an already-dumped grouping so snapshots can be merged."""
        for row in rows:
            key = row.get(key_name)
            if key is None:
                continue
            name = str(key)
            _merge_metrics(self.buckets.setdefault(name, _zero_metrics()), row)
            known = self.labels.setdefault(name, {})
            for label, value in row.items():
                if label == key_name or label in _METRIC_KEYS:
                    continue
                if label.startswith("duration_ms"):
                    continue
                if value is not None and label not in known:
                    known[label] = value

    def dump(self, key_name: str, sort_key: str | None = None) -> list[dict[str, Any]]:
        rows = [
            {key_name: key, **self.labels.get(key, {}), **_round_metrics(metrics)}
            for key, metrics in self.buckets.items()
        ]
        if sort_key:
            rows.sort(key=lambda r: (r.get(sort_key) is None, r.get(sort_key) or 0, r[key_name]))
        else:
            rows.sort(key=lambda r: (-r["records"], r[key_name]))
        return rows


_GROUPINGS: tuple[tuple[str, str, str | None], ...] = (
    ("by_stage", "stage_id", "stage_ordinal"),
    ("by_agent", "agent_id", None),
    ("by_event_type", "event_type", None),
    ("by_model", "model", None),
    ("by_outcome", "outcome", None),
    ("by_escalation_reason", "reason", None),
    ("by_human_gate_decision", "decision", None),
)


def empty_snapshot() -> dict[str, Any]:
    """The starting point a first refresh merges into."""
    return {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "generated_at": None,
        "refresh_count": 0,
        "cursor": None,
        "window": {"first_record_at": None, "last_record_at": None},
        "source": {},
        "totals": _round_metrics(_zero_metrics()) | {"cases": 0, "traces": 0},
        "identity": {"case_ids": [], "trace_ids": []},
        **{name: [] for name, _, _ in _GROUPINGS},
    }


def aggregate_records(
    records: Iterable[Mapping[str, Any]],
    *,
    stage_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Roll a batch of STS records into a snapshot-shaped delta.

    ``stage_labels`` is injected by the caller that owns the process vocabulary;
    without it stages are reported by id, never by a guessed name.
    """
    labels = stage_labels or {}
    totals = _zero_metrics()
    groups = {name: _Grouping() for name, _, _ in _GROUPINGS}
    case_ids: set[str] = set()
    trace_ids: set[str] = set()
    source: dict[str, Any] = {}
    first_at: str | None = None
    last_at: str | None = None
    cursor: str | None = None

    for record in records:
        _accumulate(totals, record)

        stage_id = record.get(A_STAGE_ID)
        groups["by_stage"].add(
            stage_id,
            record,
            stage_ordinal=record.get(A_STAGE_ORDINAL),
            stage_label=labels.get(str(stage_id)) if stage_id else None,
        )
        groups["by_agent"].add(
            record.get(A_AGENT_ID), record, agent_type=record.get(A_AGENT_TYPE)
        )
        groups["by_event_type"].add(record.get(A_EVENT_TYPE), record)
        # Attribute against the model that actually answered, since a fleet may
        # serve a request on a different one than it asked for.
        groups["by_model"].add(
            record.get(A_RESPONSE_MODEL) or record.get(A_REQUEST_MODEL),
            record,
            requested_as=record.get(A_REQUEST_MODEL),
        )
        groups["by_outcome"].add(record.get(A_OUTCOME), record)
        groups["by_escalation_reason"].add(
            record.get(A_ESCALATION_REASON),
            record,
            routed_to=record.get(A_ESCALATION_ROUTED_TO),
        )
        groups["by_human_gate_decision"].add(
            record.get(A_HITL_DECISION), record, actor_role=record.get(A_HITL_ROLE)
        )

        if record.get(A_CASE_ID):
            case_ids.add(str(record[A_CASE_ID]))
        if record.get(A_TRACE_ID):
            trace_ids.add(str(record[A_TRACE_ID]))

        timestamp = record.get(A_TIMESTAMP)
        if isinstance(timestamp, str):
            first_at = timestamp if first_at is None or timestamp < first_at else first_at
            last_at = timestamp if last_at is None or timestamp > last_at else last_at
        # Fill each identity field from the first record that actually carries
        # it, rather than taking them all from the first record. Not every
        # emitter sets every field: one that serves no single process emits no
        # process name, and a batch that happens to start with such a record
        # would otherwise report no process at all.
        for field, attribute in (
            ("sts_schema_version", A_SCHEMA_VERSION),
            ("process_name", A_PROCESS_NAME),
            ("process_version", A_PROCESS_VERSION),
            ("tenant_id", A_TENANT),
            ("environment", A_ENVIRONMENT),
        ):
            if source.get(field) is None and record.get(attribute) is not None:
                source[field] = record[attribute]
        cursor = _advance_cursor(cursor, record)

    return {
        "schema": AGGREGATE_SCHEMA,
        "schema_version": AGGREGATE_SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "refresh_count": 1,
        "cursor": cursor,
        "window": {"first_record_at": first_at, "last_record_at": last_at},
        "source": source,
        "totals": _round_metrics(totals) | {"cases": len(case_ids), "traces": len(trace_ids)},
        "identity": {"case_ids": sorted(case_ids), "trace_ids": sorted(trace_ids)},
        **{
            name: groups[name].dump(key_name, sort_key)
            for name, key_name, sort_key in _GROUPINGS
        },
    }


def _advance_cursor(cursor: str | None, record: Mapping[str, Any]) -> str | None:
    """Monotonic high-water mark: timestamp plus sequence when the stream is
    sequenced, the timestamp alone when it is not."""
    timestamp = record.get(A_TIMESTAMP)
    if not isinstance(timestamp, str):
        return cursor
    sequence = record.get("bridge.seq")
    candidate = f"{timestamp}|{int(sequence):012d}" if isinstance(sequence, int) else timestamp
    if cursor is None or candidate > cursor:
        return candidate
    return cursor


def record_is_after(record: Mapping[str, Any], cursor: str | None) -> bool:
    """Whether a record falls after a snapshot cursor, so a refresh running over
    an overlapping stream does not count anything twice."""
    if cursor is None:
        return True
    position = _advance_cursor(None, record)
    return position is not None and position > cursor


def merge_snapshots(previous: Mapping[str, Any], delta: Mapping[str, Any]) -> dict[str, Any]:
    """Add a delta onto a banked snapshot. Counters sum, maxima take the larger,
    identity sets union, and the window widens to cover both."""
    merged = empty_snapshot()

    totals = _zero_metrics()
    _merge_metrics(totals, previous.get("totals", {}))
    _merge_metrics(totals, delta.get("totals", {}))

    previous_identity = previous.get("identity", {})
    delta_identity = delta.get("identity", {})
    case_ids = set(previous_identity.get("case_ids", [])) | set(
        delta_identity.get("case_ids", [])
    )
    trace_ids = set(previous_identity.get("trace_ids", [])) | set(
        delta_identity.get("trace_ids", [])
    )

    for name, key_name, sort_key in _GROUPINGS:
        grouping = _Grouping()
        grouping.load(previous.get(name, []), key_name)
        grouping.load(delta.get(name, []), key_name)
        merged[name] = grouping.dump(key_name, sort_key)

    def _window(field: str, pick: Any) -> str | None:
        values = [
            value
            for value in (
                previous.get("window", {}).get(field),
                delta.get("window", {}).get(field),
            )
            if isinstance(value, str)
        ]
        return pick(values) if values else None

    merged["generated_at"] = delta.get("generated_at") or _utc_now_iso()
    merged["refresh_count"] = int(_as_number(previous.get("refresh_count"))) + 1
    merged["cursor"] = max(
        [c for c in (previous.get("cursor"), delta.get("cursor")) if isinstance(c, str)],
        default=None,
    )
    merged["window"] = {
        "first_record_at": _window("first_record_at", min),
        "last_record_at": _window("last_record_at", max),
    }
    merged["source"] = {
        **previous.get("source", {}),
        **{k: v for k, v in delta.get("source", {}).items() if v is not None},
    }
    merged["totals"] = _round_metrics(totals) | {
        "cases": len(case_ids),
        "traces": len(trace_ids),
    }
    merged["identity"] = {"case_ids": sorted(case_ids), "trace_ids": sorted(trace_ids)}
    return merged
