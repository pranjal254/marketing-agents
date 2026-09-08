"""Business-day arithmetic for review SLAs — deterministic, weekend-aware
(mirrors the Agent 4 sweep semantics so SLA math is consistent across agents)."""

from __future__ import annotations

from datetime import date, timedelta


def business_days_between(start: date, end: date) -> int:
    """Whole business days from start to end (0 when end <= start)."""
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def add_business_days(start: date, days: int) -> date:
    """The date ``days`` business days after ``start`` (skips weekends)."""
    cursor = start
    remaining = max(days, 0)
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor
