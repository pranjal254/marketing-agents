"""Step 7 (deterministic): back-planned content calendar + workflow plan.

Pure date math — no LLM. Back-plans from the campaign window start (the package
must be complete at launch; Phase 1's terminal output is a locked package handed to
Phase 2), honoring:
- flagship-first sequencing (derivatives draft only after flagship confirm);
- every human review gate at full configured length — an infeasible window
  produces an explicit trade-off report, review gates are NEVER compressed;
- the 2-researched-blogs/month capacity rule across registered campaigns.

Every schedule entry carries its back-planning constraint chain (spec
Explainability: every calendar date cites its constraint chain).
"""

from __future__ import annotations

from datetime import date, timedelta

from c2c_campaign_box.agent_config import OrchestratorConfig
from c2c_campaign_box.models import (
    AssetChecklistItem,
    InfeasibilityReport,
    ScheduleEntry,
    WorkflowPlan,
)


def add_business_days(start: date, days: int) -> date:
    """Add (or subtract, days<0) whole business days, skipping weekends."""
    step = 1 if days >= 0 else -1
    remaining = abs(days)
    current = start
    while remaining > 0:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= 1
    return current


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def back_plan(
    config: OrchestratorConfig,
    checklist_items: list[AssetChecklistItem],
    *,
    campaign_id: str,
    window_start: str,
    window_end: str,
    plan_date: date,
    existing_researched_blog_months: list[str],
    version: int = 1,
) -> WorkflowPlan:
    launch = date.fromisoformat(window_start)
    gates = config.review_gates
    drafting = config.drafting_business_days

    flagship_items = [
        i for i in checklist_items if config.item_for(i.asset_type).review_gate == "flagship"
    ]
    derivative_items = [
        i for i in checklist_items if config.item_for(i.asset_type).review_gate != "flagship"
    ]

    # Back-planning chain (derivatives end at launch; flagship must confirm before
    # derivative drafting starts — flagship-first sequencing).
    derivative_confirm = launch
    derivative_draft_due = add_business_days(derivative_confirm, -gates.derivative_business_days)
    derivative_draft_start = add_business_days(derivative_draft_due, -drafting.derivative)
    flagship_confirm = derivative_draft_start
    flagship_draft_due = add_business_days(flagship_confirm, -gates.flagship_business_days)
    flagship_draft_start = add_business_days(flagship_draft_due, -drafting.flagship)

    entries: list[ScheduleEntry] = []
    flagship_chain = (
        f"launch {launch.isoformat()} ← derivatives confirm at launch ← derivative review "
        f"{gates.derivative_business_days}bd ← derivative drafting {drafting.derivative}bd ← "
        f"flagship confirm {flagship_confirm.isoformat()} (flagship-first) ← flagship review "
        f"{gates.flagship_business_days}bd ← flagship draft due {flagship_draft_due.isoformat()}"
    )
    for item in flagship_items:
        entries.append(
            ScheduleEntry(
                asset_id=item.asset_id,
                asset_type=item.asset_type,
                draft_due=flagship_draft_due.isoformat(),
                review_due=flagship_confirm.isoformat(),
                confirm_due=flagship_confirm.isoformat(),
                review_gate="flagship",
                constraint_chain=flagship_chain,
            )
        )
    derivative_chain = (
        f"launch {launch.isoformat()} ← derivative review {gates.derivative_business_days}bd "
        f"← draft due {derivative_draft_due.isoformat()} (drafting starts after flagship "
        f"confirm {flagship_confirm.isoformat()})"
    )
    for item in derivative_items:
        entries.append(
            ScheduleEntry(
                asset_id=item.asset_id,
                asset_type=item.asset_type,
                draft_due=derivative_draft_due.isoformat(),
                review_due=derivative_confirm.isoformat(),
                confirm_due=derivative_confirm.isoformat(),
                review_gate="derivative",
                constraint_chain=derivative_chain,
            )
        )

    # Feasibility: the whole back-planned chain must start today or later.
    reasons: list[str] = []
    trade_offs: list[str] = []
    if flagship_draft_start < plan_date:
        earliest_launch = launch + timedelta(days=(plan_date - flagship_draft_start).days)
        reasons.append(
            "back-planned flagship drafting would need to start "
            f"{flagship_draft_start.isoformat()}, before the planning date "
            f"{plan_date.isoformat()} — the window cannot hold every review gate "
            "at full length"
        )
        trade_offs.append(
            f"shift the campaign start to {earliest_launch.isoformat()} or later "
            "(keeps every review gate at full length)"
        )
        trade_offs.append("reduce the derivative asset volume so drafting rounds run in parallel")
        trade_offs.append(
            "Marketing Lead may explicitly waive/shorten a review gate (capacity-rule "
            "override — a recorded human decision, never applied silently)"
        )

    # Capacity rule: researched blogs per calendar month, across registered campaigns.
    cap = config.capacity.researched_blogs_per_month
    own_blog_months = [
        _month_key(flagship_draft_due)
        for i in flagship_items
        if config.item_for(i.asset_type).is_researched_blog
    ]
    capacity_note = (
        f"capacity rule: {cap} researched blogs/month; this campaign adds "
        f"{len(own_blog_months)} in {own_blog_months[0] if own_blog_months else 'n/a'}"
    )
    for month in set(own_blog_months):
        existing = sum(1 for m in existing_researched_blog_months if m == month)
        added = sum(1 for m in own_blog_months if m == month)
        if existing + added > cap:
            reasons.append(
                f"researched-blog capacity exceeded for {month}: {existing} already planned "
                f"+ {added} from this campaign > cap {cap}"
            )
            trade_offs.append(
                f"move the flagship drafting out of {month}, or request an explicit "
                "capacity-rule override from the Marketing Lead"
            )

    feasible = not reasons
    return WorkflowPlan(
        campaign_id=campaign_id,
        version=version,
        window_start=window_start,
        window_end=window_end,
        entries=entries,
        feasible=feasible,
        infeasibility=(
            None if feasible else InfeasibilityReport(reasons=reasons, trade_offs=trade_offs)
        ),
        capacity_note=capacity_note,
    )
