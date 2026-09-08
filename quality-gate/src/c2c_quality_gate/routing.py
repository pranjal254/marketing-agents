"""Steps 7-9: the deterministic routing module — a policy lookup state machine,
NO LLM anywhere in this file.

Distribution class per asset comes from the versioned routing policy (never
inference); the review sequence per class is fixed; the package sign-off task is
created alongside but can only be decided after every asset task is approved —
sequence integrity is structural (guardrail 3): the order check lives in
``ordered_gate_open``, and urgency shortens SLAs, never the gate list."""

from __future__ import annotations

import uuid
from datetime import date

from c2c_campaign_box.models import PackageManifest

from c2c_quality_gate.agent_config import QualityGateConfig
from c2c_quality_gate.business_days import add_business_days, business_days_between
from c2c_quality_gate.models import ReviewTask, SweepOutcome


def _task_id() -> str:
    return f"rt-{uuid.uuid4().hex[:8]}"


def build_asset_tasks(
    config: QualityGateConfig,
    manifest: PackageManifest,
    asset_ids: list[str],
    *,
    today: date,
    created_at: str,
) -> list[ReviewTask]:
    """Review tasks for the given gate-passed assets — the full sequence for each
    asset's distribution class. Pure — persistence is the caller's."""
    tasks: list[ReviewTask] = []
    assets = {a.asset_id: a for a in manifest.assets}
    for asset_id in asset_ids:
        asset = assets[asset_id]
        cls = config.class_for(asset.asset_type)
        for index, step in enumerate(config.sequence_for(cls)):
            tasks.append(
                ReviewTask(
                    task_id=_task_id(),
                    campaign_id=manifest.campaign_id,
                    scope="asset",
                    asset_id=asset_id,
                    step=step.step,
                    role=step.role,
                    sequence_index=index,
                    sla_business_days=step.sla_business_days,
                    due=add_business_days(today, step.sla_business_days).isoformat(),
                    created_at=created_at,
                )
            )
    return tasks


def build_package_task(
    config: QualityGateConfig, campaign_id: str, *, today: date, created_at: str
) -> ReviewTask:
    """The single BU Campaign Lead package sign-off task — decidable only after
    every asset task is approved (``ordered_gate_open``)."""
    signoff = config.package_signoff
    return ReviewTask(
        task_id=_task_id(),
        campaign_id=campaign_id,
        scope="package",
        step=signoff.step,
        role=signoff.role,
        sequence_index=0,
        sla_business_days=signoff.sla_business_days,
        due=add_business_days(today, signoff.sla_business_days).isoformat(),
        created_at=created_at,
    )


def ordered_gate_open(task: ReviewTask, all_tasks: list[ReviewTask]) -> str | None:
    """Sequence-integrity check (structural, guardrail 3). Returns the violation
    description, or None when the task is legitimately next in line."""
    if task.status != "open":
        return f"task {task.task_id} is already {task.status}"
    if task.scope == "asset":
        earlier = [
            t for t in all_tasks
            if t.scope == "asset" and t.asset_id == task.asset_id
            and t.sequence_index < task.sequence_index and t.status != "approved"
        ]
        if earlier:
            return (
                f"step {task.step!r} cannot be decided before "
                f"{', '.join(t.step for t in earlier)} on {task.asset_id!r}"
            )
        return None
    pending = [
        t for t in all_tasks if t.scope == "asset" and t.status not in ("approved", "cancelled")
    ]
    if pending:
        return (
            "package sign-off requires every asset review approved first "
            f"({len(pending)} still open)"
        )
    return None


def plan_reminders(
    tasks: list[ReviewTask], config: QualityGateConfig, today: date
) -> SweepOutcome:
    """Pure reminder/escalation planning per task SLA: reminder 1 at the first
    threshold (default 50% elapsed), reminder 2 at the second (90%), escalation
    once past due. Monotonic — nothing is ever re-sent. The caller applies the
    counters and emits telemetry."""
    outcome = SweepOutcome()
    thresholds = sorted(config.reminder_thresholds)[:2]
    for task in tasks:
        if task.status != "open":
            continue
        created = date.fromisoformat(task.created_at[:10])
        due = date.fromisoformat(task.due)
        total = max(business_days_between(created, due), 1)
        elapsed = business_days_between(created, today)
        share = elapsed / total
        if today > due and not task.escalated:
            outcome.reviews_escalated += 1
            outcome.details.append(f"escalate:{task.task_id}")
        elif len(thresholds) > 1 and share >= thresholds[1] and task.reminders_sent < 2:
            outcome.reminders_sent += 1
            outcome.details.append(f"remind2:{task.task_id}")
        elif thresholds and share >= thresholds[0] and task.reminders_sent < 1:
            outcome.reminders_sent += 1
            outcome.details.append(f"remind1:{task.task_id}")
    return outcome
