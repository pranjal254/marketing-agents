"""Agent 5 (Quality Gate & Approval) routes for the dev bridge.

The bridge plays Execution Studio's event role: it hands the package manifest to
the gate, carries identity-stamped human review decisions (Grammar QA, BU Lead
package sign-off, disputes, reviewer-caught misses) into the agent, runs the SLA
sweep on demand, and exposes gate state + compliance reports to the studio. The
gate itself never edits content and no route can approve anything without a
human identity."""

from __future__ import annotations

from typing import Any, Literal

from c2c_quality_gate import persistence as gate_db
from c2c_quality_gate.orchestration import (
    GateSequencingError,
    GateStateError,
    QualityGateAgent,
)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class GateDecisionIn(BaseModel):
    decision: Literal["approved", "returned"]
    actor_id: str = Field(min_length=1)
    actor_role: str = "grammar-quality-reviewer"
    notes: str = ""
    disputed_rule_ids: list[str] = Field(default_factory=list)
    return_asset_ids: list[str] = Field(default_factory=list)


class FalseNegativeIn(BaseModel):
    asset_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    reviewer_id: str = Field(min_length=1)
    reviewer_role: str = "grammar-quality-reviewer"
    notes: str = ""


def register_gate_routes(app: FastAPI, bridge: Any) -> None:
    """``bridge`` is the zero-arg accessor returning the live Bridge instance."""

    def gate() -> QualityGateAgent:
        return bridge().gate  # type: ignore[no-any-return]

    @app.post("/api/box/campaigns/{campaign_id}/gate/run")
    def run_gate(campaign_id: str) -> dict[str, Any]:
        """Package manifest hand-off → the gate. Re-running after rework is the
        delta re-check (unchanged assets reuse their reports)."""
        try:
            with bridge().run_lock:
                outcome = gate().run_gate(campaign_id)
        except (GateStateError, GateSequencingError) as exc:
            raise _gate_error(exc) from exc
        return outcome.model_dump()

    @app.get("/api/box/campaigns/{campaign_id}/gate")
    def gate_detail(campaign_id: str) -> dict[str, Any]:
        store = bridge().store
        state = gate_db.load_gate_state(store, campaign_id)
        return {
            "state": state.model_dump() if state else None,
            "reports": [r.model_dump() for r in gate_db.load_reports(store, campaign_id)],
            "tasks": [t.model_dump() for t in gate_db.load_tasks(store, campaign_id)],
            "approvals": [
                a.model_dump() for a in gate_db.load_approvals(store, campaign_id)
            ],
            "calibration": [
                e.model_dump() for e in gate_db.load_calibration(store, campaign_id)
            ],
        }

    @app.post("/api/box/campaigns/{campaign_id}/gate/tasks/{task_id}/decision")
    def review_decision(
        campaign_id: str, task_id: str, body: GateDecisionIn
    ) -> dict[str, Any]:
        """THE human gates: Grammar QA and BU Lead sign-off, identity-stamped.
        Approving the package sign-off locks and releases the package."""
        try:
            with bridge().run_lock:
                task = gate().record_review_outcome(
                    campaign_id, task_id,
                    decision=body.decision,
                    actor_id=body.actor_id, actor_role=body.actor_role,
                    notes=body.notes,
                    disputed_rule_ids=body.disputed_rule_ids or None,
                    return_asset_ids=body.return_asset_ids or None,
                )
        except (GateStateError, GateSequencingError) as exc:
            raise _gate_error(exc) from exc
        return task.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/gate/false-negative")
    def false_negative(campaign_id: str, body: FalseNegativeIn) -> dict[str, Any]:
        """A reviewer caught a violation the gate missed — calibration + alert."""
        try:
            with bridge().run_lock:
                event = gate().record_false_negative(
                    campaign_id, body.asset_id,
                    rule_id=body.rule_id,
                    reviewer_id=body.reviewer_id, reviewer_role=body.reviewer_role,
                    notes=body.notes,
                )
        except (GateStateError, GateSequencingError) as exc:
            raise _gate_error(exc) from exc
        return event.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/gate/sweep")
    def gate_sweep(campaign_id: str) -> dict[str, Any]:
        with bridge().run_lock:
            outcome = gate().sweep(campaign_id)
        return outcome.model_dump()

    @app.post("/api/box/campaigns/{campaign_id}/gate/verify-locks")
    def verify_locks(campaign_id: str) -> dict[str, Any]:
        with bridge().run_lock:
            violated = gate().verify_locks(campaign_id)
        return {"violated": violated}


def _gate_error(exc: Exception) -> HTTPException:
    status = 404 if "unknown review task" in str(exc) else 409
    return HTTPException(status_code=status, detail=str(exc))
