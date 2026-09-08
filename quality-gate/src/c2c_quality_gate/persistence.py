"""Context Store persistence for Agent 5 — versioned, append-only records
(compliance reports, package gate state, review tasks, approval records,
calibration events). Every kind is registered in the governance catalog
(migration 0004)."""

from __future__ import annotations

from datetime import UTC, datetime

from shiftai_shared.context_store.store import ContextStore

from c2c_quality_gate.models import (
    ApprovalRecord,
    AssetReport,
    CalibrationEvent,
    PackageGateState,
    ReviewTask,
)

KIND_ASSET_REPORT = "compliance_report"
KIND_PACKAGE_GATE = "package_gate_state"
KIND_REVIEW_TASK = "approval_review_task"
KIND_APPROVAL = "approval_record"
KIND_CALIBRATION = "calibration_event"


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def save_report(store: ContextStore, report: AssetReport) -> None:
    store.put(
        KIND_ASSET_REPORT, f"{report.campaign_id}:{report.asset_id}", report.model_dump()
    )


def load_report(store: ContextStore, campaign_id: str, asset_id: str) -> AssetReport | None:
    record = store.get(KIND_ASSET_REPORT, f"{campaign_id}:{asset_id}")
    return AssetReport.model_validate(record.value) if record else None


def load_reports(store: ContextStore, campaign_id: str) -> list[AssetReport]:
    out: list[AssetReport] = []
    for record in store.query(KIND_ASSET_REPORT):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(AssetReport.model_validate(record.value))
    out.sort(key=lambda r: r.asset_id)
    return out


def save_gate_state(store: ContextStore, state: PackageGateState) -> None:
    store.put(KIND_PACKAGE_GATE, state.campaign_id, state.model_dump())


def load_gate_state(store: ContextStore, campaign_id: str) -> PackageGateState | None:
    record = store.get(KIND_PACKAGE_GATE, campaign_id)
    return PackageGateState.model_validate(record.value) if record else None


def save_task(store: ContextStore, task: ReviewTask) -> None:
    store.put(KIND_REVIEW_TASK, f"{task.campaign_id}:{task.task_id}", task.model_dump())


def load_tasks(store: ContextStore, campaign_id: str) -> list[ReviewTask]:
    out: list[ReviewTask] = []
    for record in store.query(KIND_REVIEW_TASK):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(ReviewTask.model_validate(record.value))
    out.sort(key=lambda t: (t.scope, t.asset_id, t.sequence_index))
    return out


def load_task(store: ContextStore, campaign_id: str, task_id: str) -> ReviewTask | None:
    record = store.get(KIND_REVIEW_TASK, f"{campaign_id}:{task_id}")
    return ReviewTask.model_validate(record.value) if record else None


def save_approval(store: ContextStore, approval: ApprovalRecord) -> None:
    store.put(
        KIND_APPROVAL,
        f"{approval.campaign_id}:{approval.approval_id}",
        approval.model_dump(),
    )


def load_approvals(store: ContextStore, campaign_id: str) -> list[ApprovalRecord]:
    out: list[ApprovalRecord] = []
    for record in store.query(KIND_APPROVAL):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(ApprovalRecord.model_validate(record.value))
    out.sort(key=lambda a: a.at)
    return out


def save_calibration(store: ContextStore, event: CalibrationEvent) -> None:
    store.put(
        KIND_CALIBRATION,
        f"{event.campaign_id}:{event.event_id}",
        event.model_dump(),
    )


def load_calibration(store: ContextStore, campaign_id: str) -> list[CalibrationEvent]:
    out: list[CalibrationEvent] = []
    for record in store.query(KIND_CALIBRATION):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(CalibrationEvent.model_validate(record.value))
    out.sort(key=lambda e: e.at)
    return out
