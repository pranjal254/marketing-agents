"""Task 3 — duplicate and timing-conflict detection against the campaign calendar
in the Context Store. Deterministic and fully explainable: every flag cites the
conflicting campaign_id and the overlap rationale.

Freshness (kit precedent-decay discipline): a calendar record older than the decay
window is flagged 'stale' and treated as advisory only — it never hard-blocks on its
own. Fresh duplicates route to a human decision (guardrail 4: the agent proposes and
flags; duplicate handling is a human decision).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from campaign_identification.models import CampaignRequest, ConflictFlag

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "a", "an", "for", "of", "and", "in", "on", "to", "with", "campaign"}
DUPLICATE_SIMILARITY_THRESHOLD = 0.5


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def topic_similarity(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _windows_overlap(a_start: Any, a_end: Any, b_start: Any, b_end: Any) -> bool:
    s1, e1, s2, e2 = (_parse_date(v) for v in (a_start, a_end, b_start, b_end))
    if s1 is None or s2 is None:
        return False
    e1 = e1 or s1
    e2 = e2 or s2
    return s1 <= e2 and s2 <= e1


def _freshness(entry: dict[str, Any], decay_days: int, now: datetime) -> str:
    created = entry.get("created_at") or entry.get("registered_at")
    created_date = _parse_date(created)
    if created_date is None:
        return "fresh"
    age = (now.date() - created_date).days
    return "stale" if age > decay_days else "fresh"


def detect_conflicts(
    request: CampaignRequest,
    calendar_entries: list[dict[str, Any]],
    *,
    decay_days: int,
    now: datetime | None = None,
) -> list[ConflictFlag]:
    """Compare the request against open/scheduled campaigns (BU + vertical + topic +
    audience window), per spec Task 3."""
    now = now or datetime.now(tz=UTC)
    flags: list[ConflictFlag] = []
    for entry in calendar_entries:
        if str(entry.get("status", "open")).lower() in ("closed", "completed", "cancelled"):
            continue
        same_bu = (
            request.business_unit
            and str(entry.get("business_unit", "")).strip().lower()
            == request.business_unit.strip().lower()
        )
        same_vertical = (
            request.vertical
            and str(entry.get("vertical", "")).strip().lower() == request.vertical.strip().lower()
        )
        if not (same_bu and same_vertical):
            continue
        campaign_id = str(entry.get("campaign_id", entry.get("id", "unknown")))
        similarity = topic_similarity(request.offer_topic, str(entry.get("topic", "")))
        overlap = _windows_overlap(
            request.timeline_start,
            request.timeline_end,
            entry.get("window_start"),
            entry.get("window_end"),
        )
        freshness = _freshness(entry, decay_days, now)
        if similarity >= DUPLICATE_SIMILARITY_THRESHOLD and overlap:
            flags.append(
                ConflictFlag(
                    kind="duplicate",
                    conflicting_campaign_id=campaign_id,
                    rationale=(
                        f"same BU '{request.business_unit}' + vertical '{request.vertical}', "
                        f"topic similarity {similarity:.2f}, overlapping window"
                    ),
                    freshness=freshness,  # type: ignore[arg-type]
                )
            )
        elif overlap and _audience_overlap(request, entry):
            flags.append(
                ConflictFlag(
                    kind="timing",
                    conflicting_campaign_id=campaign_id,
                    rationale=(
                        f"same BU '{request.business_unit}' + vertical '{request.vertical}' "
                        "targeting an overlapping audience window"
                    ),
                    freshness=freshness,  # type: ignore[arg-type]
                )
            )
    return flags


def _audience_overlap(request: CampaignRequest, entry: dict[str, Any]) -> bool:
    entry_audience = str(entry.get("audience", entry.get("target_segment", ""))).strip().lower()
    return bool(
        request.target_segment
        and entry_audience
        and entry_audience == request.target_segment.strip().lower()
    )


def blocking_duplicates(flags: list[ConflictFlag]) -> list[ConflictFlag]:
    """Only fresh duplicate flags force the human-decision path; stale ones ride on
    the brief as advisory context."""
    return [f for f in flags if f.kind == "duplicate" and f.freshness == "fresh"]
