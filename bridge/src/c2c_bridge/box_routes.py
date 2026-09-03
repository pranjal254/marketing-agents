"""Agent 2 (Campaign-in-a-Box Orchestrator) routes for the dev bridge.

The bridge plays Execution Studio's role: it triggers planning on brief approval,
carries the Marketing Lead's identity-stamped confirmations, and stands in for
Agents 3-4 by registering content-confirmed assets (clearly a DEV stand-in — in
production that signal comes only from the Content Collaboration Agent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

from c2c_campaign_box import MODEL_ID as BOX_MODEL_ID
from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import RegisteredAsset
from c2c_campaign_box.orchestration import CampaignBoxOrchestrator, PlanGateError
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from shiftai_shared.m365.word import DocSection, DocSpec, build_docx


class BoxBridgeState(Protocol):
    @property
    def orchestrator(self) -> CampaignBoxOrchestrator: ...

    @property
    def box_workspace_dir(self) -> Path: ...

    @property
    def run_lock(self) -> Any: ...


class PlanIn(BaseModel):
    actor_id: str = Field(min_length=1)


class ConfirmIn(BaseModel):
    kind: Literal["pack", "plan"]
    decision: Literal["confirmed", "modified"] = "confirmed"
    actor_id: str = Field(min_length=1)
    actor_role: str = "marketing-lead"
    deltas: dict[str, Any] | None = None
    notes: str | None = None


class AssetConfirmIn(BaseModel):
    """DEV stand-in for the Content Collaboration Agent's content-confirmed signal."""

    actor_id: str = Field(min_length=1)
    actor_role: str = "content-reviewer"
    text: str = "Reviewed and confirmed content (dev stand-in for Agents 3-4)."
    claim_refs: list[str] = Field(default_factory=list)


class ReopenIn(BaseModel):
    asset_ids: list[str] = Field(min_length=1)
    requesting_gate: str = "quality-gate"
    actor_id: str = Field(min_length=1)
    actor_role: str = "quality-gate"
    notes: str | None = None


def _plan_summary(key: str, value: dict[str, Any]) -> dict[str, Any]:
    return {
        "campaign_id": key,
        "status": value.get("status"),
        "pack_version": value.get("pack_version"),
        "plan_version": value.get("plan_version"),
        "manifest_version": value.get("manifest_version", 0),
        "confirmations": value.get("confirmations", {}),
        "escalations": value.get("escalations", []),
        "folder": value.get("folder"),
        "trace_id": value.get("trace_id"),
        "updated_at": value.get("updated_at"),
        "reopened_assets": value.get("reopened_assets", []),
    }


def _asset_docx(asset_id: str, text: str) -> bytes:
    return build_docx(
        DocSpec(
            title=f"Confirmed asset: {asset_id}",
            subtitle="Dev stand-in content — production drafts come from Agent 3",
            sections=(DocSection(heading="Content", paragraphs=(text,)),),
        )
    )


def register_box_routes(app: FastAPI, bridge: Any) -> None:
    """``bridge`` is the zero-arg accessor returning the live Bridge instance."""

    def orchestrator() -> CampaignBoxOrchestrator:
        return bridge().box  # type: ignore[no-any-return]

    def store() -> Any:
        return bridge().store

    # ------------------------------------------------------------------ actions

    @app.post("/api/box/campaigns/{campaign_id}/plan")
    def plan_campaign(campaign_id: str, body: PlanIn) -> dict[str, Any]:
        """Planning pass, triggered on brief approval. The Agent 1 case's trace_id
        is reused so Execution Studio-style journey reconstruction spans both
        agents on one trace."""
        trace_id: str | None = None
        for record in store().query("case"):
            if record.value.get("campaign_id") == campaign_id:
                trace_id = str(record.value.get("trace_id")) or None
                break
        with bridge().run_lock:
            outcome = orchestrator().plan_campaign(campaign_id, trace_id=trace_id)
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/confirm")
    def confirm(campaign_id: str, body: ConfirmIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = orchestrator().confirm(
                    campaign_id,
                    body.kind,
                    decision=body.decision,
                    actor_id=body.actor_id,
                    actor_role=body.actor_role,
                    deltas=body.deltas,
                    notes=body.notes,
                )
        except PlanGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/assets/{asset_id}/confirm")
    def confirm_asset(campaign_id: str, asset_id: str, body: AssetConfirmIn) -> dict[str, Any]:
        """Content-confirm one checklist asset (the human decision Agent 4 will
        carry in production). When Agent 3 has staged a real draft for the asset,
        its ACTUAL bytes + claim lineage are registered; the synthetic document is
        only a fallback for assets Agent 3 does not draft (e.g. reuse decisions)."""
        from c2c_content_repurposing import persistence as rp_db

        case = box_db.load_plan_case(store(), campaign_id) or {}
        slug = str(case.get("campaign_slug", "campaign"))
        prior = [
            a.version for a in box_db.load_registered_assets(store(), campaign_id)
            if a.asset_id == asset_id
        ]
        content = _asset_docx(asset_id, body.text)
        claim_refs = list(body.claim_refs)
        staged = rp_db.latest_draft(store(), campaign_id, asset_id)
        draft_version = 0
        if staged is not None and staged.status == "staged" and staged.file_ref:
            content = bridge().repurposer.deps.workspace.download(staged.file_ref)
            draft_version = staged.version
            claim_refs = claim_refs or staged.claim_lineage or [
                m.source_ref for m in staged.claim_markers
            ]
        # The confirmed copy versions PAST both prior registrations and Agent 3's
        # staged drafts, so its canonical filename never collides in drafts/.
        version = max([*prior, draft_version, 0]) + 1
        filename = f"{slug}-{asset_id.replace('_', '-')}-v{version}.docx"
        try:
            with bridge().run_lock:
                asset: RegisteredAsset = orchestrator().register_confirmed_asset(
                    campaign_id,
                    asset_id,
                    filename=filename,
                    content=content,
                    actor_id=body.actor_id,
                    actor_role=body.actor_role,
                    claim_refs=claim_refs,
                    version=version,
                )
        except PlanGateError as exc:
            raise _gate_error(exc) from exc
        return asset.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/package")
    def run_packaging(campaign_id: str) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = orchestrator().run_packaging(campaign_id)
        except PlanGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/reopen")
    def reopen(campaign_id: str, body: ReopenIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = orchestrator().reopen_assets(
                    campaign_id,
                    body.asset_ids,
                    requesting_gate=body.requesting_gate,
                    actor_id=body.actor_id,
                    actor_role=body.actor_role,
                    notes=body.notes,
                )
        except PlanGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    # ------------------------------------------------------------------ reads

    @app.get("/api/box/campaigns")
    def list_campaign_plans() -> list[dict[str, Any]]:
        records = store().query(box_db.KIND_PLAN_CASE)
        summaries = [_plan_summary(r.key, r.value) for r in records]
        summaries.sort(key=lambda s: str(s.get("updated_at", "")), reverse=True)
        return summaries

    @app.get("/api/box/campaigns/{campaign_id}")
    def plan_detail(campaign_id: str) -> dict[str, Any]:
        case = box_db.load_plan_case(store(), campaign_id)
        if case is None:
            raise HTTPException(status_code=404, detail=f"no campaign plan for {campaign_id}")

        def latest(kind: str) -> dict[str, Any] | None:
            record = store().get(kind, campaign_id)
            return dict(record.value) if record else None

        assets = [a.model_dump() for a in box_db.load_registered_assets(store(), campaign_id)]
        return {
            "summary": _plan_summary(campaign_id, case),
            "case": case,
            "pack": latest(box_db.KIND_PACK),
            "checklist": latest(box_db.KIND_CHECKLIST),
            "outlines": (latest(box_db.KIND_OUTLINES) or {}).get("outlines", []),
            "plan": latest(box_db.KIND_WORKFLOW_PLAN),
            "manifest": latest(box_db.KIND_MANIFEST),
            "completeness_report": latest(box_db.KIND_COMPLETENESS_REPORT),
            "registered_assets": assets,
            "model": BOX_MODEL_ID,
        }

    @app.get("/api/box/documents")
    def box_document(path: str) -> FileResponse:
        """Serve a file from the campaign workspace by workspace-relative path.
        Resolution is confined to the workspace root."""
        root = bridge().box_workspace_dir.resolve()
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="document not found")
        media = (
            "text/csv"
            if target.suffix == ".csv"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if target.suffix == ".docx"
            else "application/octet-stream"
        )
        return FileResponse(target, filename=target.name, media_type=media)


def _gate_error(exc: PlanGateError) -> HTTPException:
    status = 404 if str(exc).startswith("unknown campaign") else 409
    return HTTPException(status_code=status, detail=str(exc))
