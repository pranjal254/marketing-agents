"""Task 9 + fallback persistence: cases, gap requests, intake context, approval tasks,
campaign-calendar registration, and structured failure records (a request is never
discarded). All writes go through the append-only Context Store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from shiftai_shared.context_store.store import ContextStore

from campaign_identification.models import (
    ApprovalRecord,
    CampaignBrief,
    CampaignRequest,
    GapRequest,
    IntakeContext,
)

KIND_CASE = "case"
KIND_GAP_REQUEST = "gap_request"
KIND_INTAKE_CONTEXT = "intake_context"
KIND_APPROVAL_TASK = "approval_task"
KIND_APPROVED_BRIEF = "approved_brief"
KIND_CALENDAR = "campaign_calendar"
KIND_FAILED_REQUEST = "failed_request"
KIND_HUMAN_DECISION = "human_decision"


class Workspace(Protocol):
    """Campaign workspace binding: OneDrive in production, local folder in dev/tests."""

    def upload_document(self, filename: str, content: bytes) -> str:
        """Store the document; returns an external reference (item id / path)."""
        ...


class LocalWorkspace:
    def __init__(self, root: str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def upload_document(self, filename: str, content: bytes) -> str:
        target = self._root / filename
        target.write_bytes(content)
        return str(target)


class OneDriveWorkspace:
    """Production binding over the shared OneDrive connector."""

    def __init__(self, connector: Any, drive_id: str, folder_id: str) -> None:
        self._connector = connector
        self._drive_id = drive_id
        self._folder_id = folder_id

    def upload_document(self, filename: str, content: bytes) -> str:
        item = self._connector.upload_bytes(
            self._drive_id,
            self._folder_id,
            filename,
            content,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
        return str(item.get("id", item.get("webUrl", filename)))


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def save_case(store: ContextStore, case_id: str, state: dict[str, Any]) -> None:
    store.put(KIND_CASE, case_id, {**state, "updated_at": _now()})


def load_case(store: ContextStore, case_id: str) -> dict[str, Any] | None:
    record = store.get(KIND_CASE, case_id)
    return record.value if record else None


def save_gap_request(store: ContextStore, gap: GapRequest) -> None:
    store.put(KIND_GAP_REQUEST, gap.case_id, gap.model_dump())


def save_intake_context(store: ContextStore, context: IntakeContext) -> None:
    store.put(KIND_INTAKE_CONTEXT, context.case_id, context.model_dump())


def save_approval_task(
    store: ContextStore, case_id: str, brief: CampaignBrief, doc_ref: str, routed_to: str
) -> dict[str, Any]:
    task = {
        "task_id": f"task_{case_id}",
        "case_id": case_id,
        "campaign_id": brief.campaign_id,
        "brief_version": brief.version,
        "doc_ref": doc_ref,
        "routed_to": routed_to,
        "created_at": _now(),
        "status": "open",
    }
    store.put(KIND_APPROVAL_TASK, case_id, task)
    return task


def save_approved_brief(store: ContextStore, brief: CampaignBrief, doc_ref: str) -> None:
    store.put(
        KIND_APPROVED_BRIEF,
        brief.campaign_id,
        {"brief": brief.model_dump(), "doc_ref": doc_ref, "released_at": _now()},
    )


def register_campaign(store: ContextStore, brief: CampaignBrief) -> None:
    """On approval the campaign enters the calendar so future intake sees it."""
    fields = {f.name: f.value for f in brief.fields}
    store.put(
        KIND_CALENDAR,
        brief.campaign_id,
        {
            "campaign_id": brief.campaign_id,
            "business_unit": fields.get("business_unit"),
            "vertical": fields.get("vertical"),
            "topic": fields.get("offer_topic"),
            "audience": fields.get("target_segment"),
            "window_start": fields.get("timeline_start"),
            "window_end": fields.get("timeline_end"),
            "status": "open",
            "created_at": _now(),
        },
    )


def load_calendar(store: ContextStore) -> list[dict[str, Any]]:
    return [r.value for r in store.query(KIND_CALENDAR)]


def save_failed_request(
    store: ContextStore, case_id: str, raw: dict[str, Any], error_type: str, detail: str
) -> None:
    """Fallback (spec): persist the raw request and a structured failure record —
    never discard a request."""
    store.put(
        KIND_FAILED_REQUEST,
        case_id,
        {"raw_request": raw, "error_type": error_type, "detail": detail, "failed_at": _now()},
    )


def save_human_decision(
    store: ContextStore, case_id: str, approval: ApprovalRecord, scenario_hash: str
) -> None:
    store.put(
        KIND_HUMAN_DECISION,
        f"{case_id}:{approval.timestamp}",
        {**approval.model_dump(), "case_id": case_id, "scenario_hash": scenario_hash},
    )


def occurrence_count_90d(store: ContextStore, scenario_hash: str) -> int:
    cutoff = datetime.now(tz=UTC) - timedelta(days=90)
    count = 0
    for record in store.query(KIND_HUMAN_DECISION):
        if record.value.get("scenario_hash") != scenario_hash:
            continue
        try:
            ts = datetime.fromisoformat(str(record.value["timestamp"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= cutoff:
            count += 1
    return count


def request_from_case(case: dict[str, Any]) -> CampaignRequest:
    return CampaignRequest.model_validate(case["request"])
