"""Task 3 — duplicate/timing detection with explainable citations + freshness decay
(kit precedent-freshness discipline mapped to calendar-entry age)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from campaign_identification.conflicts import (
    blocking_duplicates,
    detect_conflicts,
    topic_similarity,
)
from campaign_identification.intake import normalize_request


def _request() -> object:
    return normalize_request(
        {
            "business_unit": "Technology",
            "vertical": "manufacturing",
            "topic": "ERP modernization assessment for manufacturers",
            "target_segment": "type_3",
            "timeline_start": "2026-10-01",
            "timeline_end": "2026-11-15",
        },
        "form",
    )


def _calendar_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "campaign_id": "cmp_existing",
        "business_unit": "Technology",
        "vertical": "manufacturing",
        "topic": "ERP modernization assessment campaign",
        "audience": "type_3",
        "window_start": "2026-10-15",
        "window_end": "2026-11-30",
        "status": "open",
        "created_at": datetime.now(tz=UTC).date().isoformat(),
    }
    entry.update(overrides)
    return entry


def test_duplicate_flag_cites_conflicting_campaign() -> None:
    flags = detect_conflicts(_request(), [_calendar_entry()], decay_days=90)  # type: ignore[arg-type]
    assert len(flags) == 1
    flag = flags[0]
    assert flag.kind == "duplicate"
    assert flag.conflicting_campaign_id == "cmp_existing"
    assert "topic similarity" in flag.rationale
    assert flag.freshness == "fresh"
    assert blocking_duplicates(flags) == flags


def test_stale_entry_is_advisory_not_blocking() -> None:
    old = (datetime.now(tz=UTC) - timedelta(days=120)).date().isoformat()
    flags = detect_conflicts(
        _request(),
        [_calendar_entry(created_at=old)],
        decay_days=90,  # type: ignore[arg-type]
    )
    assert flags and flags[0].freshness == "stale"
    assert blocking_duplicates(flags) == []


def test_different_bu_or_vertical_not_flagged() -> None:
    assert (
        detect_conflicts(_request(), [_calendar_entry(business_unit="Retail")], decay_days=90) == []  # type: ignore[arg-type]
    )
    assert (
        detect_conflicts(_request(), [_calendar_entry(vertical="technology")], decay_days=90) == []  # type: ignore[arg-type]
    )


def test_timing_conflict_same_audience_window() -> None:
    entry = _calendar_entry(topic="completely different subject matter offer")
    flags = detect_conflicts(_request(), [entry], decay_days=90)  # type: ignore[arg-type]
    assert len(flags) == 1
    assert flags[0].kind == "timing"


def test_no_overlap_no_flag() -> None:
    entry = _calendar_entry(window_start="2027-01-01", window_end="2027-02-01")
    assert detect_conflicts(_request(), [entry], decay_days=90) == []  # type: ignore[arg-type]


def test_closed_campaigns_ignored() -> None:
    assert detect_conflicts(_request(), [_calendar_entry(status="closed")], decay_days=90) == []  # type: ignore[arg-type]


def test_topic_similarity_bounds() -> None:
    assert topic_similarity("ERP modernization", "ERP modernization") == 1.0
    assert topic_similarity("ERP modernization", "cloud security webinar") < 0.2
    assert topic_similarity(None, "x") == 0.0
