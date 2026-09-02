"""Context Store persistence for the orchestrator — versioned, append-only records
(pack, checklist, plan, asset registry, manifest, confirmations, failures)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shiftai_shared.context_store.store import ContextStore

from c2c_campaign_box.models import (
    AssetChecklist,
    AudienceOfferPack,
    CompletenessReport,
    ConfirmationRecord,
    PackageManifest,
    RegisteredAsset,
    WorkflowPlan,
)

KIND_PLAN_CASE = "plan_case"
KIND_PACK = "audience_offer_pack"
KIND_CHECKLIST = "asset_checklist"
KIND_OUTLINES = "content_outlines"
KIND_WORKFLOW_PLAN = "workflow_plan"
KIND_PLANNED_ASSET = "planned_asset"
KIND_REGISTERED_ASSET = "registered_asset"
KIND_CONFIRMATION = "confirmation"
KIND_MANIFEST = "package_manifest"
KIND_COMPLETENESS_REPORT = "completeness_report"
KIND_FAILED_RUN = "failed_plan_run"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def save_plan_case(store: ContextStore, campaign_id: str, state: dict[str, Any]) -> None:
    store.put(KIND_PLAN_CASE, campaign_id, {**state, "updated_at": _now()})


def load_plan_case(store: ContextStore, campaign_id: str) -> dict[str, Any] | None:
    record = store.get(KIND_PLAN_CASE, campaign_id)
    return record.value if record else None


def save_pack(store: ContextStore, pack: AudienceOfferPack) -> None:
    store.put(KIND_PACK, pack.campaign_id, pack.model_dump())


def save_checklist(store: ContextStore, checklist: AssetChecklist) -> None:
    store.put(KIND_CHECKLIST, checklist.campaign_id, checklist.model_dump())


def save_outlines(store: ContextStore, campaign_id: str, outlines: list[dict[str, Any]]) -> None:
    store.put(KIND_OUTLINES, campaign_id, {"outlines": outlines, "created_at": _now()})


def save_workflow_plan(store: ContextStore, plan: WorkflowPlan) -> None:
    store.put(KIND_WORKFLOW_PLAN, plan.campaign_id, plan.model_dump())


def register_planned_asset(
    store: ContextStore,
    campaign_id: str,
    asset_id: str,
    *,
    asset_type: str,
    is_researched_blog: bool,
    draft_month: str,
) -> None:
    """Step 7: every planned asset enters the Context Store; researched-blog months
    feed the fleet-wide capacity check."""
    store.put(
        KIND_PLANNED_ASSET,
        f"{campaign_id}:{asset_id}",
        {
            "campaign_id": campaign_id,
            "asset_id": asset_id,
            "asset_type": asset_type,
            "is_researched_blog": is_researched_blog,
            "draft_month": draft_month,
            "created_at": _now(),
        },
    )


def researched_blog_months(store: ContextStore, *, exclude_campaign: str) -> list[str]:
    months: list[str] = []
    for record in store.query(KIND_PLANNED_ASSET):
        value = record.value
        if value.get("campaign_id") == exclude_campaign:
            continue
        if value.get("is_researched_blog"):
            months.append(str(value.get("draft_month", "")))
    return [m for m in months if m]


def register_asset(store: ContextStore, campaign_id: str, asset: RegisteredAsset) -> None:
    store.put(KIND_REGISTERED_ASSET, f"{campaign_id}:{asset.asset_id}", asset.model_dump())


def load_registered_assets(store: ContextStore, campaign_id: str) -> list[RegisteredAsset]:
    out: list[RegisteredAsset] = []
    for record in store.query(KIND_REGISTERED_ASSET):
        if record.value.get("asset_id") and record.key.startswith(f"{campaign_id}:"):
            out.append(RegisteredAsset.model_validate(record.value))
    return out


def save_confirmation(
    store: ContextStore, campaign_id: str, record: ConfirmationRecord
) -> None:
    store.put(
        KIND_CONFIRMATION,
        f"{campaign_id}:{record.kind}:{record.timestamp}",
        {**record.model_dump(), "campaign_id": campaign_id},
    )


def save_manifest(store: ContextStore, manifest: PackageManifest) -> None:
    store.put(KIND_MANIFEST, manifest.campaign_id, manifest.model_dump())


def save_completeness_report(store: ContextStore, report: CompletenessReport) -> None:
    store.put(KIND_COMPLETENESS_REPORT, report.campaign_id, report.model_dump())


def save_failed_run(
    store: ContextStore, campaign_id: str, error_type: str, detail: str
) -> None:
    """A planning/packaging failure is persisted, never discarded."""
    store.put(
        KIND_FAILED_RUN,
        f"{campaign_id}:{_now()}",
        {"campaign_id": campaign_id, "error_type": error_type, "detail": detail},
    )
