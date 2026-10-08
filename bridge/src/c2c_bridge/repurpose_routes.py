"""Agent 3 (Content Repurposing) routes for the dev bridge.

The bridge plays Execution Studio's role: it triggers the flagship draft on
outline approval, carries the Content Writer's identity-stamped flagship
confirmation (a DEV stand-in for the Content Collaboration Agent's
content-confirmed signal), triggers the fan-out that confirmation unlocks, and
routes rework requests. The sequencing guardrail lives in the agent — the bridge
cannot fan out an unconfirmed flagship any more than a user can.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import AssetChecklist
from c2c_content_repurposing import MODEL_ID as REPURPOSE_MODEL_ID
from c2c_content_repurposing import persistence as rp_db
from c2c_content_repurposing.content_settings import (
    ContentSettings,
    apply_requests,
    defaults_for_checklist,
)
from c2c_content_repurposing.orchestration import (
    ContentRepurposingAgent,
    RepurposeGateError,
    SequencingViolationError,
)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from shiftai_shared.llm import effective_model


class FlagshipIn(BaseModel):
    actor_id: str = Field(min_length=1)


class FlagshipConfirmIn(BaseModel):
    """DEV stand-in for Agent 4's content-confirmed signal — a human decision,
    identity-stamped, carried by the bridge."""

    actor_id: str = Field(min_length=1)
    actor_role: str = "content-writer"
    notes: str | None = None


class AssetSettingIn(BaseModel):
    """One asset's requested counts and length. Every field but the id is
    optional so a partial update is a legal request."""

    asset_id: str = Field(min_length=1)
    variants: int | None = Field(default=None, ge=1)
    min_words: int | None = Field(default=None, ge=1)
    max_words: int | None = Field(default=None, ge=1)


class ContentSettingsIn(BaseModel):
    """The Content Writer's settings for a campaign, applied between confirming
    the flagship and triggering the fan-out."""

    actor_id: str = Field(min_length=1)
    actor_role: str = "content-writer"
    note: str | None = None
    items: list[AssetSettingIn] = Field(default_factory=list)


class ReworkIn(BaseModel):
    asset_id: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: str = "content-reviewer"
    rule_codes: list[str] = Field(default_factory=list)


def _draft_view(draft: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    """Store record → API view with workspace-relative refs for downloads."""

    def rel(ref: str | None) -> str | None:
        if not ref:
            return None
        try:
            return Path(ref).resolve().relative_to(workspace_root.resolve()).as_posix()
        except ValueError:
            return None

    return {
        **draft,
        "file_rel": rel(draft.get("file_ref")),
        "claim_map_rel": rel(draft.get("claim_map_ref")),
    }


def register_repurpose_routes(app: FastAPI, bridge: Any) -> None:
    """``bridge`` is the zero-arg accessor returning the live Bridge instance."""

    def agent() -> ContentRepurposingAgent:
        return bridge().repurposer  # type: ignore[no-any-return]

    def store() -> Any:
        return bridge().store

    @app.post("/api/box/campaigns/{campaign_id}/flagship")
    def draft_flagship(campaign_id: str, body: FlagshipIn) -> dict[str, Any]:
        """Flagship drafting, triggered on outline approval (plan confirmed).
        The agent reuses the campaign's existing trace id for journey continuity;
        the staged draft immediately enters Agent 4's review cycle."""
        from c2c_bridge.review_routes import stage_new_drafts

        try:
            with bridge().run_lock:
                outcome = agent().draft_flagship(campaign_id)
                stage_new_drafts(bridge, campaign_id)
        except RepurposeGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/flagship/confirm")
    def confirm_flagship(campaign_id: str, body: FlagshipConfirmIn) -> dict[str, Any]:
        """The flagship content_confirmed gate — now Agent 4's: the review agent
        records the identity-stamped decision; its signal binding unlocks Agent
        3's fan-out and registers the REAL flagship bytes with packaging."""
        from c2c_collaboration.models import ReviewState
        from c2c_collaboration.orchestration import ReviewGateError

        flagship_id = agent().deps.config.flagship_asset_type
        try:
            with bridge().run_lock:
                state: ReviewState = bridge().collab.confirm_content(
                    campaign_id, flagship_id,
                    actor_id=body.actor_id, actor_role=body.actor_role,
                )
        except ReviewGateError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RepurposeGateError as exc:
            raise _gate_error(exc) from exc
        return state.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/fanout")
    def run_fanout(campaign_id: str) -> dict[str, Any]:
        """Derivative fan-out — only possible after the human confirmation above
        (the agent's state machine refuses anything else). Every staged
        derivative enters Agent 4's review cycle."""
        from c2c_bridge.review_routes import stage_new_drafts

        try:
            with bridge().run_lock:
                outcome = agent().run_fanout(campaign_id)
                stage_new_drafts(bridge, campaign_id)
        except SequencingViolationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RepurposeGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/rework")
    def rework(campaign_id: str, body: ReworkIn) -> dict[str, Any]:
        from c2c_bridge.review_routes import stage_new_drafts

        try:
            with bridge().run_lock:
                outcome = agent().apply_rework(
                    campaign_id, body.asset_id,
                    instruction=body.instruction, actor_id=body.actor_id,
                    actor_role=body.actor_role, rule_codes=body.rule_codes,
                )
                stage_new_drafts(bridge, campaign_id)
        except RepurposeGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    def _checklist(campaign_id: str) -> AssetChecklist:
        record = store().get(box_db.KIND_CHECKLIST, campaign_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"no approved asset plan for {campaign_id!r} yet",
            )
        return AssetChecklist.model_validate(record.value)

    def _settings_view(
        campaign_id: str, settings: ContentSettings, saved: bool
    ) -> dict[str, Any]:
        """Settings plus the bounds the studio needs to constrain its inputs,
        so the UI and the agent cannot disagree about what is allowed."""
        limits = agent().deps.config.content_limits
        return {
            "campaign_id": campaign_id,
            "saved": saved,
            "settings": settings.model_dump(),
            "limits": {
                "max_variants_per_asset": limits.max_variants_per_asset,
                "word_floor": limits.word_floor,
                "word_ceiling": limits.word_ceiling,
            },
            "config_version": agent().deps.config.version,
        }

    @app.get("/api/box/campaigns/{campaign_id}/content-settings")
    def content_settings(campaign_id: str) -> dict[str, Any]:
        """What generation will use. Falls back to the plan volumes and the
        config word ranges until the writer saves something."""
        saved = rp_db.load_content_settings(store(), campaign_id)
        if saved is not None:
            return _settings_view(campaign_id, saved, saved=True)
        checklist = _checklist(campaign_id)
        defaults = defaults_for_checklist(
            campaign_id, list(checklist.items), agent().deps.config
        )
        return _settings_view(campaign_id, defaults, saved=False)

    @app.put("/api/box/campaigns/{campaign_id}/content-settings")
    def save_content_settings(
        campaign_id: str, body: ContentSettingsIn
    ) -> dict[str, Any]:
        """Identity-stamped and append-only: each save is a new version, and
        anything beyond the configured ceiling comes back clamped with a note
        rather than being rejected or applied silently."""
        existing = rp_db.load_content_settings(store(), campaign_id)
        if existing is None:
            checklist = _checklist(campaign_id)
            baseline = defaults_for_checklist(
                campaign_id, list(checklist.items), agent().deps.config
            )
            next_version = 1
        else:
            baseline = existing
            next_version = existing.version + 1

        updated = apply_requests(
            campaign_id,
            next_version,
            [item.model_dump(exclude_none=True) for item in body.items],
            baseline,
            agent().deps.config,
            set_by=body.actor_id,
            set_by_role=body.actor_role,
            note=body.note,
        )
        with bridge().run_lock:
            rp_db.save_content_settings(store(), updated)
        return _settings_view(campaign_id, updated, saved=True)

    @app.get("/api/box/campaigns/{campaign_id}/drafts")
    def drafts(campaign_id: str) -> dict[str, Any]:
        case = rp_db.load_case(store(), campaign_id)
        if case is None and box_db.load_plan_case(store(), campaign_id) is None:
            # Unknown campaign (e.g. a session from before a bridge restart) —
            # a 404 here lets the studio stop polling instead of looping forever.
            raise HTTPException(
                status_code=404, detail=f"no campaign plan for {campaign_id}"
            )
        root = bridge().box_workspace_dir
        draft_records = [
            _draft_view(d.model_dump(), root)
            for d in rp_db.load_drafts(store(), campaign_id)
        ]
        inventory = None
        if case and case.get("inventory_version"):
            record = rp_db.load_inventory(
                store(), campaign_id, int(case["inventory_version"])
            )
            inventory = record.model_dump() if record else None
        return {
            "case": case,
            "status": (case or {}).get("status"),
            "drafts": draft_records,
            "inventory": inventory,
            "gap_notes": [g.model_dump() for g in rp_db.load_gap_notes(store(), campaign_id)],
            # Why the last run failed, in words a marketer can act on. The studio
            # renders it next to the retry button instead of a bare "failed".
            "last_error": (
                {
                    "error_type": case.get("error_type"),
                    "detail": case.get("error_detail"),
                    "hint": case.get("error_hint"),
                }
                if case and case.get("status") == "failed" and case.get("error_type")
                else None
            ),
            # What actually answered, not the id the spec routes to.
            "model": effective_model(bridge().agent.deps.provider, REPURPOSE_MODEL_ID),
            "target_model": REPURPOSE_MODEL_ID,
        }


def _gate_error(exc: RepurposeGateError) -> HTTPException:
    status = 404 if str(exc).startswith("unknown repurpose case") else 409
    return HTTPException(status_code=status, detail=str(exc))
