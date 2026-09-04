"""Step 1: reviewer assignment from the workflow plan.

Reviewers come from the versioned config as ROLES (editorial → Content Writer,
message fit → Marketing Lead); person resolution happens at the surface (studio
in dev, Execution Studio in prod). Due dates come from the Campaign-in-a-Box
workflow plan's review entries — the calendar authority."""

from __future__ import annotations

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import AssetChecklist, WorkflowPlan
from shiftai_shared.context_store.store import ContextStore

from c2c_collaboration.agent_config import CollaborationConfig, ReviewGate
from c2c_collaboration.models import ReviewerAssignment, ReviewState
from c2c_collaboration.persistence import now_iso


class AssignmentError(Exception):
    """The asset cannot be assigned (unknown asset, missing plan)."""


def build_assignment(
    store: ContextStore,
    config: CollaborationConfig,
    campaign_id: str,
    asset_id: str,
    *,
    draft_version: int,
) -> ReviewState:
    checklist_record = store.get(box_db.KIND_CHECKLIST, campaign_id)
    if checklist_record is None:
        raise AssignmentError(f"no asset checklist exists for {campaign_id!r}")
    checklist = AssetChecklist.model_validate(checklist_record.value)
    item = next((i for i in checklist.items if i.asset_id == asset_id), None)
    if item is None:
        raise AssignmentError(f"asset {asset_id!r} is not on the checklist")

    gate: ReviewGate = (
        "flagship" if item.asset_type == config.flagship_asset_type else "derivative"
    )
    due = ""
    plan_record = store.get(box_db.KIND_WORKFLOW_PLAN, campaign_id)
    if plan_record is not None:
        plan = WorkflowPlan.model_validate(plan_record.value)
        entry = next((e for e in plan.entries if e.asset_id == asset_id), None)
        if entry is not None:
            due = entry.review_due

    return ReviewState(
        campaign_id=campaign_id,
        asset_id=asset_id,
        asset_type=item.asset_type,
        review_gate=gate,
        reviewers=[
            ReviewerAssignment(role=slot.role, focus=slot.focus)
            for slot in config.reviewers_for(gate)
        ],
        due=due,
        status="in_review",
        rounds=0,
        draft_version=draft_version,
        staged_at=now_iso(),
        created_at=now_iso(),
    )
