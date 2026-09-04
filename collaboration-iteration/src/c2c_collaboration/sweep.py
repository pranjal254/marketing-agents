"""Step 9: stale-asset sweep — deterministic, no LLM.

Graduated ladder against the review due date (business days): first reminder at
due, second at +1bd, escalation to the Marketing Lead at +2bd with the blocking
reviewer roles and the asset's age. Invokable (bridge endpoint / CLI); the
4-business-hour scheduler binds at Execution Studio onboarding."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from c2c_collaboration.agent_config import CollaborationConfig
from c2c_collaboration.models import ReviewState


@dataclass(frozen=True)
class SweepAction:
    campaign_id: str
    asset_id: str
    action: str  # "remind" | "escalate"
    reminder_number: int
    business_days_overdue: int
    blocking_roles: list[str]


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


def plan_sweep(
    states: list[ReviewState], config: CollaborationConfig, today: date
) -> list[SweepAction]:
    """Pure planning: which assets get a reminder or an escalation today.
    Reminders are monotonic (never re-sent); an escalated asset is not re-escalated."""
    ladder = config.reminder_ladder
    actions: list[SweepAction] = []
    for state in states:
        if state.status == "content_confirmed" or not state.due:
            continue
        try:
            due = date.fromisoformat(state.due)
        except ValueError:
            continue
        overdue = business_days_between(due, today)
        if today < due or (overdue == 0 and today != due):
            continue
        blocking = [r.role for r in state.reviewers]
        if overdue >= ladder.escalate_bd and not state.escalated:
            actions.append(SweepAction(state.campaign_id, state.asset_id, "escalate",
                                       state.reminders_sent, overdue, blocking))
        elif overdue >= ladder.second_reminder_bd and state.reminders_sent < 2:
            actions.append(SweepAction(state.campaign_id, state.asset_id, "remind",
                                       2, overdue, blocking))
        elif overdue >= ladder.first_reminder_bd and state.reminders_sent < 1:
            actions.append(SweepAction(state.campaign_id, state.asset_id, "remind",
                                       1, overdue, blocking))
    return actions
