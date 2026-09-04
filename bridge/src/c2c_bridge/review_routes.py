"""Agent 4 (Collaboration & Iteration) routes for the dev bridge.

The bridge plays Execution Studio's event role: it stages drafts into review as
Agent 3 produces them, carries reviewer comments + identity-stamped human
decisions (feedback-complete, conflict resolutions, content_confirmed) into the
agent, and exposes the review state to the studio. The old dev stand-ins are
retired: confirmation now flows through Agent 4 and its signal bindings."""

from __future__ import annotations

from typing import Any

from c2c_collaboration import persistence as review_db
from c2c_collaboration.orchestration import CollaborationAgent, ReviewGateError
from c2c_content_repurposing import persistence as rp_db
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class FeedbackIn(BaseModel):
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str = "content-writer"
    section: str = ""
    text: str = Field(min_length=1)


class RoundIn(BaseModel):
    actor_id: str = Field(min_length=1)


class ResolveIn(BaseModel):
    decision: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    actor_role: str = "marketing-lead"


class ConfirmIn(BaseModel):
    actor_id: str = Field(min_length=1)
    actor_role: str = "content-writer"


def stage_new_drafts(bridge: Any, campaign_id: str) -> None:
    """Execution Studio's 'draft staged' event, played by the bridge: every staged
    Agent 3 draft enters (or refreshes) Agent 4's review state. Idempotent per
    draft version; assets already content_confirmed are left alone."""
    collab: CollaborationAgent = bridge().collab
    seen: set[str] = set()
    for draft in rp_db.load_drafts(bridge().store, campaign_id):
        if draft.asset_id in seen or draft.status != "staged":
            continue
        seen.add(draft.asset_id)
        try:
            collab.on_draft_staged(campaign_id, draft.asset_id)
        except ReviewGateError:
            continue  # confirmed assets / re-open path — not this event's business


def register_review_routes(app: FastAPI, bridge: Any) -> None:
    """``bridge`` is the zero-arg accessor returning the live Bridge instance."""

    def collab() -> CollaborationAgent:
        return bridge().collab  # type: ignore[no-any-return]

    @app.post("/api/box/campaigns/{campaign_id}/assets/{asset_id}/feedback")
    def add_feedback(campaign_id: str, asset_id: str, body: FeedbackIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                item = collab().add_feedback(
                    campaign_id, asset_id,
                    reviewer_id=body.reviewer_id, reviewer_role=body.reviewer_role,
                    section=body.section, text=body.text,
                )
        except ReviewGateError as exc:
            raise _gate_error(exc) from exc
        return item.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/assets/{asset_id}/feedback-complete")
    def run_round(campaign_id: str, asset_id: str, body: RoundIn) -> dict[str, Any]:
        """Reviewer signals done → one consolidation + revision round."""
        try:
            with bridge().run_lock:
                outcome = collab().run_review_round(
                    campaign_id, asset_id, actor_id=body.actor_id
                )
                stage_new_drafts(bridge, campaign_id)  # revised version re-enters review
        except ReviewGateError as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/assets/{asset_id}/conflicts/{conflict_id}/resolve")
    def resolve_conflict(
        campaign_id: str, asset_id: str, conflict_id: str, body: ResolveIn
    ) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                record = collab().resolve_conflict(
                    campaign_id, asset_id, conflict_id,
                    decision=body.decision, actor_id=body.actor_id,
                    actor_role=body.actor_role,
                )
        except ReviewGateError as exc:
            raise _gate_error(exc) from exc
        return record.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/assets/{asset_id}/review-confirm")
    def confirm_content(campaign_id: str, asset_id: str, body: ConfirmIn) -> dict[str, Any]:
        """THE human gate: content_confirmed with identity. Signals fire from the
        agent (flagship → fan-out unlock; derivative → packaging registration)."""
        try:
            with bridge().run_lock:
                state = collab().confirm_content(
                    campaign_id, asset_id,
                    actor_id=body.actor_id, actor_role=body.actor_role,
                )
        except ReviewGateError as exc:
            raise _gate_error(exc) from exc
        return state.model_dump()

    @app.get("/api/box/campaigns/{campaign_id}/review")
    def review_detail(campaign_id: str) -> dict[str, Any]:
        store = bridge().store
        with bridge().run_lock:
            stage_new_drafts(bridge, campaign_id)  # lazily heal missed stage events
        states = review_db.load_states(store, campaign_id)
        out: dict[str, Any] = {"assets": []}
        for state in states:
            asset_id = state.asset_id
            out["assets"].append({
                "state": state.model_dump(),
                "feedback": [f.model_dump()
                             for f in review_db.all_feedback(store, campaign_id, asset_id)],
                "rounds": [r.model_dump()
                           for r in review_db.load_rounds(store, campaign_id, asset_id)],
                "conflicts": [c.model_dump()
                              for c in review_db.load_conflicts(store, campaign_id, asset_id)],
            })
        return out

    @app.post("/api/box/campaigns/{campaign_id}/sweep")
    def sweep(campaign_id: str) -> dict[str, Any]:
        """Invokable stale-asset sweep (the 4-business-hour scheduler binds at
        Execution Studio onboarding). Campaign-scoped output for the studio."""
        with bridge().run_lock:
            outcome = collab().sweep()
        prefix = f"{campaign_id}:"
        return {
            "checked": outcome.checked,
            "reminded": [x for x in outcome.reminded if x.startswith(prefix)],
            "escalated": [x for x in outcome.escalated if x.startswith(prefix)],
        }


def _gate_error(exc: ReviewGateError) -> HTTPException:
    status = 404 if str(exc).startswith("no review state") else 409
    return HTTPException(status_code=status, detail=str(exc))
