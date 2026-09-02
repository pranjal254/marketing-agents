"""STS v2.0.0 telemetry emitter.

Every record is validated against the kit schema at emit time — an invalid record is
a defect and raises, never silently dropped or "fixed". The stream is the append-only
audit trail (telemetry-standard.md §1): sinks expose emit() only; no update, no delete.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema

from shiftai_shared.config import Environment
from shiftai_shared.telemetry.schema import load_sts_schema

STS_SCHEMA_VERSION = "2.0.0"


class TelemetryValidationError(Exception):
    """An emitted record failed STS schema validation."""


class TelemetrySink:
    """Append-only sink. Deliberately no read-modify or delete surface."""

    def emit(self, record: dict[str, Any]) -> None:
        raise NotImplementedError


class InMemorySink(TelemetrySink):
    """Test sink. ``records`` returns copies so stored records stay immutable."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def emit(self, record: dict[str, Any]) -> None:
        self._records.append(dict(record))

    @property
    def records(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._records]


class JsonlSink(TelemetrySink):
    """One JSON record per line, opened in append mode only."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def emit(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class StsEmitter:
    """Builds and emits STS v2 records with the agent's static core fields baked in.

    The static fields come from the agent's Business Capability config + spec
    (agent id/type, tenant, config version, risk tier, data classification).
    """

    def __init__(
        self,
        sink: TelemetrySink,
        *,
        tenant_id: str,
        agent_id: str,
        agent_type: str,
        config_version: str,
        environment: Environment,
        risk_tier: str,
        data_classification: str,
        process_name: str | None = None,
        schema_path: str | None = None,
    ) -> None:
        self._sink = sink
        self._static: dict[str, Any] = {
            "shiftai.schema.version": STS_SCHEMA_VERSION,
            "shiftai.tenant.id": tenant_id,
            "shiftai.agent.id": agent_id,
            "shiftai.agent.type": agent_type,
            "shiftai.config.version": config_version,
            "deployment.environment.name": environment,
            "shiftai.risk.tier": risk_tier,
            "shiftai.data.classification": data_classification,
        }
        if process_name:
            self._static["shiftai.process.name"] = process_name
        schema = load_sts_schema(schema_path)
        self._validator = jsonschema.Draft202012Validator(schema)

    def validate(self, record: dict[str, Any]) -> None:
        errors = sorted(self._validator.iter_errors(record), key=lambda e: list(e.absolute_path))
        if errors:
            details = "; ".join(
                f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errors
            )
            raise TelemetryValidationError(f"STS record invalid: {details}")

    def emit(
        self,
        event_type: str,
        *,
        case_id: str,
        trace_id: str,
        timestamp: str | None = None,
        **attributes: Any,
    ) -> dict[str, Any]:
        """Emit one record. ``attributes`` are flattened STS attribute names.

        Additive (non-schema) attributes such as ``shiftai.run.id`` or the
        ``shiftai.learn.*`` namespace are legal passthrough per STS v2
        (additionalProperties: true) and Cross-Agent Standards B/C.
        """
        record: dict[str, Any] = dict(self._static)
        record["shiftai.event.type"] = event_type
        record["shiftai.case.id"] = case_id
        record["shiftai.trace.id"] = trace_id
        record["shiftai.timestamp"] = timestamp or utc_now_iso()
        for key, value in attributes.items():
            if value is not None:
                record[key] = value
        # decision_made may carry an explicit null action class (abstention).
        if event_type == "decision_made" and "shiftai.decision.action_class" not in record:
            record["shiftai.decision.action_class"] = None
        self.validate(record)
        self._sink.emit(record)
        return record
