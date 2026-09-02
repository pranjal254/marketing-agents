"""Task 1 — ingest campaign requests from the three entry points and normalize into
the common request record. Deterministic; no LLM."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from campaign_identification.models import CampaignRequest, RequestSource

# Column/key aliases seen across the intake form (Forms->Excel), the quarterly plan
# sheet, and calendar entries. Keys are canonical model fields.
_ALIASES: dict[str, tuple[str, ...]] = {
    "requester": ("requester", "requester_email", "submitted_by", "responder"),
    "objective": ("objective", "campaign_objective", "goal"),
    "business_unit": ("business_unit", "bu", "business unit"),
    "vertical": ("vertical", "industry"),
    "target_segment": ("target_segment", "segment", "target segment"),
    "offer_topic": ("offer_topic", "topic", "offer", "offer/topic", "offer or topic"),
    "timeline_start": ("timeline_start", "start_date", "start", "target_window_start"),
    "timeline_end": ("timeline_end", "end_date", "end", "target_window_end"),
    "owner": ("owner", "campaign_owner"),
    "free_text_context": ("free_text_context", "context", "notes", "comments", "description"),
}

_SEGMENT_NORMALIZE = {
    "type 3": "type_3",
    "type3": "type_3",
    "type_3": "type_3",
    "type 4": "type_4",
    "type4": "type_4",
    "type_4": "type_4",
    "standard": "standard",
}

_VERTICAL_NORMALIZE = {
    "financial services": "financial_services",
    "finserv": "financial_services",
    "financial_services": "financial_services",
    "manufacturing": "manufacturing",
    "technology": "technology",
    "tech": "technology",
}


def _first(raw: dict[str, Any], field: str) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}
    for alias in _ALIASES.get(field, (field,)):
        if alias in lowered and lowered[alias] not in (None, ""):
            return lowered[alias]
    return None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _as_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("yes", "true", "y", "1", "approved"):
        return True
    if text in ("no", "false", "n", "0"):
        return False
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_request(
    raw: dict[str, Any],
    source: RequestSource,
    *,
    source_ref: str | None = None,
    request_id: str | None = None,
) -> CampaignRequest:
    """Map one raw entry-point record onto the common request record.

    Unknown values stay None — validation turns them into gap requests; nothing is
    ever invented here (guardrail 1).
    """
    lowered = {str(k).strip().lower(): v for k, v in raw.items()}
    segment_raw = _as_str(_first(raw, "target_segment"))
    vertical_raw = _as_str(_first(raw, "vertical"))
    refs = [source_ref] if source_ref else []
    ref_field = lowered.get("source_ref") or lowered.get("row_ref")
    if ref_field:
        refs.append(str(ref_field))
    return CampaignRequest(
        request_id=request_id or f"req_{uuid.uuid4().hex[:10]}",
        source=source,
        received_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        requester=_as_str(_first(raw, "requester")),
        objective=_as_str(_first(raw, "objective")),
        business_unit=_as_str(_first(raw, "business_unit")),
        vertical=(
            _VERTICAL_NORMALIZE.get(vertical_raw.lower(), vertical_raw) if vertical_raw else None
        ),
        target_segment=(
            _SEGMENT_NORMALIZE.get(segment_raw.lower(), segment_raw) if segment_raw else None
        ),
        offer_topic=_as_str(_first(raw, "offer_topic")),
        channels=_as_list(lowered.get("channels") or lowered.get("intended_channels")),
        timeline_start=_as_str(_first(raw, "timeline_start")),
        timeline_end=_as_str(_first(raw, "timeline_end")),
        owner=_as_str(_first(raw, "owner")),
        budget_flag=_as_bool(lowered.get("budget_flag") or lowered.get("budget")),
        products=_as_list(lowered.get("products") or lowered.get("product_scope")),
        free_text_context=_as_str(_first(raw, "free_text_context")),
        source_refs=refs,
    )


def merge_gap_answers(request: CampaignRequest, answers: dict[str, Any]) -> CampaignRequest:
    """Fold requester gap answers into the request (Task 6 follow-up). Only known model
    fields can be answered; provenance is recorded by the caller."""
    normalized = normalize_request(
        {
            **request.model_dump(exclude={"request_id", "source", "received_at", "source_refs"}),
            **answers,
        },
        request.source,
        request_id=request.request_id,
    )
    return normalized.model_copy(
        update={
            "received_at": request.received_at,
            "source_refs": request.source_refs,
            "derived_fields": request.derived_fields,
        }
    )
