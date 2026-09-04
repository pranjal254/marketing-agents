"""The Collaboration & Iteration review-cycle state machine.

Per asset: staged → in_review → (rounds: collect → consolidate → classify →
apply/hold/route → summarize) → in_revision → … → content_confirmed (HUMAN only).

Structural design points:
- ``Signals`` decouples the agent from its neighbors: on flagship confirmation it
  signals the Content Repurposing Agent, on derivative confirmation the
  packaging registry, on structural feedback a consolidated rework request. The
  bridge binds these in dev; Execution Studio routes them in prod.
- ``content_confirmed`` exists ONLY inside ``confirm_content(actor_id, …)`` —
  no code path in this package invokes it (static-tested). A confirmation
  without a human identity is impossible by construction (spec alert: zero).
- Conflicts are held, never adjudicated; markers are shielded in code
  (grounding.protect_markers); no feedback item is ever dropped
  (grounding.reconcile + resolve_items cover the input exactly).

STS mapping: policy escalations carry ``shiftai.escalation.reason=policy_gap``
with the precise code in ``shiftai.learn.reason_code``; human confirmations and
conflict resolutions are ``human_gate`` records with identity and latency.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.workspace import CampaignWorkspace, WorkspaceWriteError
from c2c_content_repurposing import persistence as rp_db
from c2c_content_repurposing.models import DraftSection, StagedDraft
from c2c_content_repurposing.selfcheck import run_self_check
from shiftai_shared.brand import BrandRules
from shiftai_shared.config import SharedSettings, runtime_rate_card
from shiftai_shared.context_store.store import ContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.llm import LLMProvider, LLMResponse
from shiftai_shared.resilience import IdempotencyStore, execute_idempotent
from shiftai_shared.telemetry import StsEmitter, TelemetrySink
from shiftai_shared.telemetry.envelope import RunContext, new_id, response_cost

from c2c_collaboration import (
    AGENT_TYPE,
    DATA_CLASSIFICATION,
    MODEL_ID,
    PROCESS_NAME,
    RISK_TIER,
    RUN_TIMEOUT_S,
    SYSTEM_PROMPT_VERSION,
)
from c2c_collaboration import documents as docs
from c2c_collaboration import generation as gen
from c2c_collaboration import persistence as db
from c2c_collaboration.agent_config import CollaborationConfig
from c2c_collaboration.assignments import AssignmentError, build_assignment
from c2c_collaboration.grounding import (
    extract_conflicts,
    reconcile_consolidation,
    resolve_items,
)
from c2c_collaboration.grounding import (
    protect_markers as shield,
)
from c2c_collaboration.models import (
    ConflictRecord,
    ConsolidationLLMOutput,
    FeedbackItem,
    ItemResolution,
    IterationMetrics,
    ReviewRound,
    ReviewState,
    ReviewStatus,
    RevisedSection,
    RevisionLLMOutput,
    RoundOutcome,
    SweepOutcome,
)
from c2c_collaboration.sweep import plan_sweep


class ReviewGateError(Exception):
    """A gate violation: wrong state, unknown asset, or a forbidden transition."""


class VersionCorruptionError(Exception):
    """The asset's version chain is broken — halt the asset, page AiCoE."""


class Signals(Protocol):
    """Outbound signals (spec Connections). Bound by the bridge in dev,
    Execution Studio in production — the agent never imports its neighbors'
    orchestrators."""

    def flagship_confirmed(self, campaign_id: str, actor_id: str, actor_role: str) -> None: ...

    def register_confirmed(
        self, campaign_id: str, asset_id: str, actor_id: str, actor_role: str
    ) -> None: ...

    def route_rework(
        self, campaign_id: str, asset_id: str, instruction: str, actor_id: str
    ) -> None: ...


@dataclass
class CollaborationDeps:
    provider: LLMProvider
    store: ContextStore
    workspace: CampaignWorkspace  # the SAME campaign workspace agents 2-3 use
    sink: TelemetrySink
    kill_switch: KillSwitch
    rate_breaker: RateBreaker
    idempotency: IdempotencyStore
    config: CollaborationConfig
    settings: SharedSettings
    brand_rules: BrandRules
    signals: Signals


class CollaborationAgent:
    def __init__(self, deps: CollaborationDeps) -> None:
        self.deps = deps
        self.rate_card = runtime_rate_card(deps.settings)
        self.emitter = StsEmitter(
            deps.sink,
            tenant_id=deps.settings.shiftai_tenant_id,
            agent_id=deps.config.agent_id,
            agent_type=AGENT_TYPE,
            config_version=deps.config.version,
            environment=deps.settings.shiftai_environment,
            risk_tier=RISK_TIER,
            data_classification=DATA_CLASSIFICATION,
            process_name=PROCESS_NAME,
        )

    # ------------------------------------------------------------- step 1: stage

    def on_draft_staged(self, campaign_id: str, asset_id: str) -> ReviewState:
        """A new (or regenerated) draft entered the workspace: assign reviewers
        from the workflow plan and open/refresh the review state."""
        draft = rp_db.latest_draft(self.deps.store, campaign_id, asset_id)
        version = draft.version if draft else 0
        existing = db.load_state(self.deps.store, campaign_id, asset_id)
        if existing is not None:
            if existing.status == "content_confirmed":
                raise ReviewGateError(
                    f"{asset_id!r} is content_confirmed — re-opening goes through governance"
                )
            if existing.draft_version == version:
                return existing
            refreshed = existing.model_copy(
                update={"draft_version": version, "status": "in_review",
                        "staged_at": db.now_iso()}
            )
            db.save_state(self.deps.store, refreshed)
            self._notify_assignment(campaign_id, refreshed)
            return refreshed
        try:
            state = build_assignment(
                self.deps.store, self.deps.config, campaign_id, asset_id,
                draft_version=version,
            )
        except AssignmentError as exc:
            raise ReviewGateError(str(exc)) from exc
        db.save_state(self.deps.store, state)
        self._notify_assignment(campaign_id, state)
        return state

    def _notify_assignment(self, campaign_id: str, state: ReviewState) -> None:
        ctx = self._ctx(campaign_id)
        self._emit(
            ctx, "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "assign_reviewers",
                "shiftai.action.idempotency_key":
                    f"{campaign_id}:assign:{state.asset_id}:v{state.draft_version}",
                "shiftai.review.reviewers": json.dumps([r.role for r in state.reviewers]),
                "shiftai.review.due": state.due,
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{state.asset_id}",
            },
        )

    # ------------------------------------------------------- feedback (human in)

    def add_feedback(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        reviewer_id: str,
        reviewer_role: str,
        section: str = "",
        text: str,
    ) -> FeedbackItem:
        """One reviewer comment, carried by the surface (studio panel in dev,
        Word comments via the FeedbackSource connector later). Internal-only."""
        if not text.strip():
            raise ReviewGateError("feedback needs a non-empty comment")
        state = db.load_state(self.deps.store, campaign_id, asset_id)
        if state is None:
            state = self.on_draft_staged(campaign_id, asset_id)
        if state.status == "content_confirmed":
            raise ReviewGateError(f"{asset_id!r} is already content_confirmed")
        item = FeedbackItem(
            feedback_id=f"fb-{uuid.uuid4().hex[:8]}",
            campaign_id=campaign_id,
            asset_id=asset_id,
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            section=section.strip(),
            text=text.strip(),
            created_at=db.now_iso(),
        )
        db.save_feedback(self.deps.store, item)
        return item

    # ------------------------------------------------- steps 2-6: a review round

    def run_review_round(
        self, campaign_id: str, asset_id: str, *, actor_id: str
    ) -> RoundOutcome:
        """Feedback-complete signal: collect → consolidate → classify →
        apply/hold/route → new version + edit summary."""
        state = self._state_or_raise(campaign_id, asset_id)
        if state.status == "content_confirmed":
            raise ReviewGateError(f"{asset_id!r} is already content_confirmed")
        items = db.open_feedback(self.deps.store, campaign_id, asset_id)
        if not items:
            raise ReviewGateError(f"no open feedback for {asset_id!r} — nothing to consolidate")
        draft = rp_db.latest_draft(self.deps.store, campaign_id, asset_id)
        if draft is None:
            raise ReviewGateError(
                f"{asset_id!r} has no staged draft (reuse asset) — revisions are manual; "
                "confirm directly when ready"
            )
        self._verify_version_chain(campaign_id, asset_id)
        ctx = self._ctx(campaign_id)
        try:
            return self._round_pipeline(ctx, state, items, draft, actor_id)
        except (ReviewGateError, VersionCorruptionError):
            raise
        except Exception as exc:  # fail-closed: record, emit, never discard
            self._emit(ctx, "error", **{"error.type": type(exc).__name__,
                                        "shiftai.outcome": "failure"})
            self._emit(ctx, "run_summary", **{"shiftai.outcome": "failure",
                                              "error.type": type(exc).__name__,
                                              **self._latency_attrs(ctx)})
            raise

    def _round_pipeline(
        self,
        ctx: RunContext,
        state: ReviewState,
        items: list[FeedbackItem],
        draft: StagedDraft,
        actor_id: str,
    ) -> RoundOutcome:
        deps = self.deps
        campaign_id = state.campaign_id
        asset_id = state.asset_id
        round_n = state.rounds + 1
        escalations: list[str] = []
        self._emit(
            ctx, "case_intake",
            **{
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{asset_id}",
                "shiftai.review.round": round_n,
                "shiftai.review.feedback_items": len(items),
            },
        )
        sections_payload = [s.model_dump() for s in draft.sections]
        blocks = gen.system_blocks(deps.brand_rules)

        # ---- steps 2-3: consolidate + de-conflict (LLM proposes, code verifies)
        with ctx.span("l3-consolidation", "llm") as con_span:
            con_out, con_resp = gen.run_json_call(
                deps.provider, blocks,
                gen.consolidation_user_prompt(items, sections_payload),
                ConsolidationLLMOutput, timeout_s=RUN_TIMEOUT_S,
            )
        normalized, unclassified = reconcile_consolidation(items, con_out)
        self._emit_l3(
            ctx, con_span.span_id, con_span.duration_ms, con_resp,
            template_id=gen.CONSOLIDATION_TEMPLATE_ID, action="consolidate_feedback",
            confidence=con_out.confidence if con_out else 0.0,
            extra={
                "shiftai.review.round": round_n,
                "shiftai.review.items_in": len(items),
                "shiftai.review.unclassified": len(unclassified),
            },
        )
        if unclassified:
            escalations.append("unclassified_feedback")
            self._escalation_event(
                ctx, tier=1, reason_code="unclassified_feedback",
                detail={"feedback_ids": unclassified,
                        "note": "deferred for human review — never silently dropped"},
            )
        conflicts, conflicted_ids = extract_conflicts(
            normalized, items, campaign_id=campaign_id, asset_id=asset_id,
            round_n=round_n, created_at=db.now_iso(),
        )
        for conflict in conflicts:
            db.save_conflict(deps.store, conflict)
        if conflicts:
            escalations.append("feedback_conflict")
            self._escalation_event(
                ctx, tier=2, reason_code="feedback_conflict",
                detail={"conflicts": [c.conflict_id for c in conflicts],
                        "note": "both positions quoted and held — the agent never adjudicates"},
            )

        # ---- step 4: route structural rework as ONE consolidated instruction
        structural = [n for n in normalized
                      if n.type == "structural" and n.feedback_id not in conflicted_ids]
        structural_instruction: str | None = None
        if structural:
            structural_instruction = " ".join(
                f"[{n.feedback_id}] {n.instruction}" for n in structural
            )
            try:
                deps.signals.route_rework(campaign_id, asset_id,
                                          structural_instruction, actor_id)
            except Exception as exc:
                escalations.append("tool_failure")
                self._escalation_event(
                    ctx, tier=2, reason_code="tool_failure",
                    detail={"signal": "route_rework", "error": str(exc)},
                )

        # ---- step 5: apply textual edits (markers shielded in code)
        textual = [
            n for n in normalized
            if n.type == "textual"
            and n.feedback_id not in conflicted_ids
            and n.feedback_id not in set(unclassified)
        ]
        applied: set[str] = set()
        deferred_reasons: dict[str, str] = {}
        violations: list[str] = []
        edit_summary = ""
        safe_sections: list[RevisedSection] | None = None
        if textual:
            with ctx.span("l3-revision", "llm") as rev_span:
                rev_out, rev_resp = gen.run_json_call(
                    deps.provider, blocks,
                    gen.revision_user_prompt(
                        sections_payload,
                        [{"feedback_id": n.feedback_id, "location": n.location,
                          "instruction": n.instruction} for n in textual],
                    ),
                    RevisionLLMOutput, timeout_s=RUN_TIMEOUT_S,
                )
            self._emit_l3(
                ctx, rev_span.span_id, rev_span.duration_ms, rev_resp,
                template_id=gen.REVISION_TEMPLATE_ID, action="apply_textual_edits",
                confidence=rev_out.confidence if rev_out else 0.0,
                extra={"shiftai.review.round": round_n,
                       "shiftai.review.textual_edits": len(textual)},
            )
            if rev_out is None:
                deferred_reasons = {
                    n.feedback_id: "revision model output unparsable — deferred to humans"
                    for n in textual
                }
            else:
                original = [RevisedSection(heading=s.heading, paragraphs=list(s.paragraphs))
                            for s in draft.sections]
                safe_sections, violations = shield(original, rev_out.sections)
                applied = {i for i in rev_out.applied if i in {n.feedback_id for n in textual}}
                deferred_reasons = {
                    d.get("feedback_id", ""): d.get("reason", "")
                    for d in rev_out.deferred
                }
                edit_summary = rev_out.edit_summary
                if violations:
                    escalations.append("sourced_claim_edit")
                    self._escalation_event(
                        ctx, tier=2, reason_code="sourced_claim_edit",
                        detail={"restored_sections": violations,
                                "note": "sourced-claim sentences are human-only edits"},
                    )
        self._check_budget(ctx)

        resolutions = resolve_items(
            normalized,
            conflicted=conflicted_ids,
            applied=applied,
            deferred_reasons=deferred_reasons,
            violated_sections={v.split(":", 1)[0] for v in violations},
        )
        round_record = ReviewRound(
            campaign_id=campaign_id,
            asset_id=asset_id,
            round=round_n,
            input_feedback_ids=[i.feedback_id for i in items],
            normalized=normalized,
            resolutions=resolutions,
            conflicts=[c.conflict_id for c in conflicts],
            structural_instruction=structural_instruction,
            edit_summary=edit_summary,
            parent_version=draft.version,
            marker_violations=violations,
            created_at=db.now_iso(),
        )
        new_version: int | None = None
        if safe_sections is not None:
            _, new_version, revised_ref = self._stage_revision(
                ctx, state, draft, safe_sections, round_record,
            )
            round_record = round_record.model_copy(
                update={"new_version": new_version, "revised_ref": revised_ref}
            )
        db.save_round(deps.store, round_record)
        for item in items:  # consumed — versioned update, never deleted
            db.save_feedback(
                deps.store, item.model_copy(update={"status": "consolidated", "round": round_n})
            )

        status: ReviewStatus = ("awaiting_conflict_resolution" if conflicts
                                else "in_revision" if new_version is not None else "in_review")
        updated = state.model_copy(update={"rounds": round_n, "status": status,
                                           "draft_version": new_version or state.draft_version})
        db.save_state(deps.store, updated)
        if round_n > deps.config.max_rounds_alert:
            escalations.append("max_rounds_exceeded")
            self._escalation_event(
                ctx, tier=2, reason_code="max_rounds_exceeded",
                detail={"rounds": round_n,
                        "note": "signals an outline or brief problem, not editing"},
            )
        self._emit(
            ctx, "run_summary",
            **{
                "shiftai.outcome": "partial" if escalations else "success",
                "shiftai.review.round": round_n,
                "shiftai.review.applied": len([r for r in resolutions if r.outcome == "applied"]),
                "shiftai.review.conflicted": len(conflicted_ids),
                **self._latency_attrs(ctx),
                **(self._cost_attrs(ctx.total_cost_usd) if ctx.total_cost_usd > 0 else {}),
            },
        )
        return RoundOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            asset_id=asset_id, status=status, round=round_record, conflicts=conflicts,
            escalation_reasons=escalations,
        )

    def _stage_revision(
        self,
        ctx: RunContext,
        state: ReviewState,
        draft: StagedDraft,
        sections: list[RevisedSection],
        round_record: ReviewRound,
    ) -> tuple[StagedDraft, int, str]:
        """New version through the additive workspace — overwrites impossible.
        The rendered document carries the round's REAL edit summary and item
        resolutions (spec step 6: reviewers verify at a glance)."""
        deps = self.deps
        kill_state, breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, deps.config.agent_id,
            deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            raise ReviewGateError(f"control plane paused: {pause_reason}")
        case = box_db.load_plan_case(deps.store, state.campaign_id) or {}
        folder = str(case.get("folder", ""))
        slug = str(case.get("campaign_slug", "campaign"))
        version = draft.version + 1
        text = " ".join(p for s in sections for p in s.paragraphs)
        report = run_self_check(text, deps.brand_rules, unsourced_numeric_tokens=[])
        new_draft = draft.model_copy(update={
            "sections": [
                DraftSection(heading=s.heading, paragraphs=list(s.paragraphs))
                for s in sections
            ],
            "version": version,
            "filename": docs.revised_filename(slug, draft.asset_type, version),
            "rework_of_version": draft.version,
            "self_check": report,
            "created_at": db.now_iso(),
        })
        try:
            with ctx.span("workspace-stage-revision", "api") as span:
                ref = self._upload_once(
                    f"{state.campaign_id}:revision:{state.asset_id}:v{version}",
                    f"{folder}/drafts", new_draft.filename,
                    docs.revised_docx(new_draft, round_record),
                )
        except WorkspaceWriteError as exc:
            self._escalation_event(
                ctx, tier=3, reason_code="version_corruption",
                detail={"error": str(exc), "note": "revision upload failed — asset halted"},
                routed_override=deps.config.route_for("version_corruption"),
            )
            raise VersionCorruptionError(str(exc)) from exc
        staged = new_draft.model_copy(update={"file_ref": ref})
        rp_db.save_draft(deps.store, staged)
        self._emit(
            ctx, "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "stage_revision",
                "shiftai.action.idempotency_key":
                    f"{state.campaign_id}:revision:{state.asset_id}:v{version}",
                "shiftai.action.external_ref": ref,
                "shiftai.control.kill_switch": kill_state,
                "shiftai.control.rate_breaker": breaker_state,
                "shiftai.span.id": span.span_id,
                "shiftai.span.duration_ms": span.duration_ms,
                "shiftai.draft.version": version,
            },
        )
        deps.rate_breaker.record_execution(deps.config.agent_id)
        return staged, version, ref

    # ------------------------------------------------- human gates (never agent)

    def resolve_conflict(
        self,
        campaign_id: str,
        asset_id: str,
        conflict_id: str,
        *,
        decision: str,
        actor_id: str,
        actor_role: str = "marketing-lead",
    ) -> ConflictRecord:
        """The Marketing Lead's call. The decision becomes a new feedback item so
        the next round applies it with full attribution."""
        if not decision.strip():
            raise ReviewGateError("a conflict resolution needs a decision")
        conflict = db.load_conflict(self.deps.store, campaign_id, asset_id, conflict_id)
        if conflict is None:
            raise ReviewGateError(f"unknown conflict {conflict_id!r}")
        if conflict.status == "resolved":
            raise ReviewGateError(f"conflict {conflict_id!r} is already resolved")
        resolved = conflict.model_copy(update={
            "status": "resolved",
            "resolution": {"decision": decision.strip(), "actor_id": actor_id,
                           "actor_role": actor_role, "at": db.now_iso()},
        })
        db.save_conflict(self.deps.store, resolved)
        self.add_feedback(
            campaign_id, asset_id,
            reviewer_id=actor_id, reviewer_role=actor_role,
            section=conflict.section,
            text=f"Conflict {conflict_id} resolution: {decision.strip()}",
        )
        remaining = [c for c in db.load_conflicts(self.deps.store, campaign_id, asset_id)
                     if c.status == "open"]
        state = self._state_or_raise(campaign_id, asset_id)
        if not remaining and state.status == "awaiting_conflict_resolution":
            db.save_state(self.deps.store, state.model_copy(update={"status": "in_revision"}))
        ctx = self._ctx(campaign_id)
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "feedback_conflict",
                "shiftai.learn.agent_recommendation": "conflict_held",
                "shiftai.learn.human_action": f"resolve_conflict:{conflict_id}",
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{asset_id}",
            },
        )
        return resolved

    def confirm_content(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        actor_id: str,
        actor_role: str,
    ) -> ReviewState:
        """content_confirmed — a HUMAN action, identity-stamped. This method is
        never invoked by agent code (static-tested); the bridge/Execution Studio
        carries the reviewer's decision into it. Confirmation then signals the
        neighbors: flagship → fan-out unlock, derivative → packaging registry."""
        if not actor_id.strip():
            raise ReviewGateError("content_confirmed requires a human actor identity")
        state = db.load_state(self.deps.store, campaign_id, asset_id)
        if state is None:
            # Reuse assets have no staged draft/rounds — assignment on demand.
            state = self.on_draft_staged(campaign_id, asset_id)
        if state.status == "content_confirmed":
            raise ReviewGateError(f"{asset_id!r} is already content_confirmed")
        open_conflicts = [c for c in db.load_conflicts(self.deps.store, campaign_id, asset_id)
                          if c.status == "open"]
        if open_conflicts:
            raise ReviewGateError(
                f"{asset_id!r} has unresolved reviewer conflicts "
                f"({', '.join(c.conflict_id for c in open_conflicts)}) — the Marketing "
                "Lead resolves them before confirmation"
            )
        ctx = self._ctx(campaign_id)
        # Remaining open feedback is explicitly set aside by the confirming human
        # (spec Fallback: every item ends applied/deferred/conflicted/REJECTED-BY-HUMAN).
        leftovers = db.open_feedback(self.deps.store, campaign_id, asset_id)
        if leftovers:
            round_record = ReviewRound(
                campaign_id=campaign_id, asset_id=asset_id, round=state.rounds + 1,
                input_feedback_ids=[i.feedback_id for i in leftovers],
                resolutions=[
                    ItemResolution(feedback_id=i.feedback_id, outcome="rejected_by_human",
                                   note=f"set aside at confirmation by {actor_id}")
                    for i in leftovers
                ],
                edit_summary=(
                    f"Confirmed by {actor_id} with {len(leftovers)} open item(s) "
                    "explicitly set aside."
                ),
                parent_version=state.draft_version,
                created_at=db.now_iso(),
            )
            db.save_round(self.deps.store, round_record)
            for item in leftovers:
                db.save_feedback(
                    self.deps.store,
                    item.model_copy(update={"status": "consolidated",
                                            "round": round_record.round}),
                )
        confirmed = state.model_copy(update={
            "status": "content_confirmed",
            "confirmed_by": actor_id,
            "confirmed_role": actor_role,
            "confirmed_at": db.now_iso(),
        })
        db.save_state(self.deps.store, confirmed)
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": "approved",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "none",
                "shiftai.learn.agent_recommendation": "review_cycle_complete",
                "shiftai.learn.human_action": f"content_confirmed:{asset_id}",
                "shiftai.learn.decision_latency_ms": _elapsed_ms(state.staged_at),
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{asset_id}",
            },
        )
        self._persist_metrics(confirmed)
        # ---- step 8: signal the neighbors (decoupled; failures escalate, the
        # human decision stands — signals are retryable).
        try:
            if state.asset_type == self.deps.config.flagship_asset_type:
                self.deps.signals.flagship_confirmed(campaign_id, actor_id, actor_role)
            else:
                self.deps.signals.register_confirmed(campaign_id, asset_id,
                                                     actor_id, actor_role)
        except Exception as exc:
            self._escalation_event(
                ctx, tier=2, reason_code="tool_failure",
                detail={"signal": "confirmation", "error": str(exc),
                        "note": "confirmation recorded; downstream signal failed — retry"},
            )
        return confirmed

    def reopen_review(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        reason: str,
        actor_id: str,
        actor_role: str = "quality-gate",
    ) -> ReviewState:
        """Spec input ``gate_findings``: a downstream gate (or governance re-open)
        returns a confirmed asset into the rework cycle. Human/gate identity is
        recorded; the prior confirmation stays in the version history."""
        state = self._state_or_raise(campaign_id, asset_id)
        if state.status != "content_confirmed":
            return state  # already open — nothing to do
        reopened = state.model_copy(update={
            "status": "in_review",
            "confirmed_by": None,
            "confirmed_role": None,
            "confirmed_at": None,
            "staged_at": db.now_iso(),
        })
        db.save_state(self.deps.store, reopened)
        ctx = self._ctx(campaign_id)
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "rework_reopen",
                "shiftai.learn.agent_recommendation": "content_confirmed",
                "shiftai.learn.human_action": f"reopen_review:{asset_id}",
                "shiftai.context_package": json.dumps(
                    {"reason": reason, "actor_id": actor_id}
                ),
            },
        )
        return reopened

    # -------------------------------------------------------------- step 9: sweep

    def sweep(self, today: date | None = None) -> SweepOutcome:
        """Stale-asset sweep: graduated reminders, then escalation with the
        blocking reviewers and age. Deterministic — no LLM."""
        deps = self.deps
        now = today or datetime.now(tz=UTC).date()
        states = [s for s in db.all_states(deps.store) if s.status != "content_confirmed"]
        actions = plan_sweep(states, deps.config, now)
        by_key = {(s.campaign_id, s.asset_id): s for s in states}
        ctx = RunContext(case_id="sweep", trace_id=new_id("trace"))
        reminded: list[str] = []
        escalated: list[str] = []
        for action in actions:
            state = by_key[(action.campaign_id, action.asset_id)]
            label = f"{action.campaign_id}:{action.asset_id}"
            if action.action == "escalate":
                db.save_state(deps.store, state.model_copy(update={"escalated": True}))
                self._escalation_event(
                    self._ctx(action.campaign_id), tier=2, reason_code="stalled_asset",
                    detail={"asset_id": action.asset_id,
                            "business_days_overdue": action.business_days_overdue,
                            "blocking_reviewers": action.blocking_roles},
                )
                escalated.append(label)
            else:
                db.save_state(
                    deps.store,
                    state.model_copy(update={"reminders_sent": action.reminder_number}),
                )
                self._emit(
                    self._ctx(action.campaign_id), "action_taken",
                    **{
                        "shiftai.layer": "L4",
                        "shiftai.action.class": "send_review_reminder",
                        "shiftai.action.idempotency_key":
                            f"{label}:reminder:{action.reminder_number}",
                        "shiftai.review.reminder_number": action.reminder_number,
                        "shiftai.review.business_days_overdue": action.business_days_overdue,
                        "shiftai.business_object.type": "campaign_asset",
                        "shiftai.business_object.id": label,
                    },
                )
                reminded.append(label)
        return SweepOutcome(
            case_id="sweep", trace_id=ctx.trace_id,
            reminded=reminded, escalated=escalated, checked=len(states),
        )

    # ------------------------------------------------- step 10 + helpers

    def _persist_metrics(self, state: ReviewState) -> None:
        conflicts = db.load_conflicts(self.deps.store, state.campaign_id, state.asset_id)
        feedback = db.all_feedback(self.deps.store, state.campaign_id, state.asset_id)
        db.save_metrics(
            self.deps.store,
            IterationMetrics(
                campaign_id=state.campaign_id,
                asset_id=state.asset_id,
                rounds=state.rounds,
                feedback_items=len(feedback),
                conflicts=len(conflicts),
                staged_at=state.staged_at,
                confirmed_at=state.confirmed_at or db.now_iso(),
                time_in_review_ms=_elapsed_ms(state.staged_at),
                reminders_sent=state.reminders_sent,
                escalated=state.escalated,
            ),
        )

    def _verify_version_chain(self, campaign_id: str, asset_id: str) -> None:
        """Versions must be contiguous 1..N — corruption halts the asset (spec:
        page AiCoE)."""
        versions = sorted(
            d.version for d in rp_db.load_drafts(self.deps.store, campaign_id)
            if d.asset_id == asset_id
        )
        if versions and versions != list(range(1, len(versions) + 1)):
            self._escalation_event(
                self._ctx(campaign_id), tier=3, reason_code="version_corruption",
                detail={"asset_id": asset_id, "versions": versions},
                routed_override=self.deps.config.route_for("version_corruption"),
            )
            raise VersionCorruptionError(
                f"version chain broken for {asset_id!r}: {versions}"
            )

    def _state_or_raise(self, campaign_id: str, asset_id: str) -> ReviewState:
        state = db.load_state(self.deps.store, campaign_id, asset_id)
        if state is None:
            raise ReviewGateError(f"no review state for {campaign_id}:{asset_id}")
        return state

    def _ctx(self, campaign_id: str) -> RunContext:
        case = box_db.load_plan_case(self.deps.store, campaign_id)
        trace = str(case.get("trace_id", "")) if case else ""
        return RunContext(case_id=campaign_id, trace_id=trace or new_id("trace"))

    def _upload_once(self, key: str, folder: str, filename: str, content: bytes) -> str:
        def side_effect() -> dict[str, Any]:
            ref = self.deps.workspace.upload(folder, filename, content)
            return {"ref": ref}

        result, _ = execute_idempotent(key, self.deps.idempotency, side_effect)
        return str(result["ref"])

    def _escalation_event(
        self,
        ctx: RunContext,
        *,
        tier: int,
        reason_code: str,
        detail: dict[str, Any],
        routed_override: str | None = None,
    ) -> None:
        self._emit(
            ctx, "case_escalated",
            **{
                "shiftai.layer": "escalation",
                "shiftai.escalation.tier": tier,
                "shiftai.escalation.reason": "policy_gap",
                "shiftai.escalation.routed_to": (
                    routed_override or self.deps.config.route_for(reason_code)
                ),
                "shiftai.learn.reason_code": reason_code,
                "shiftai.context_package": json.dumps(detail, default=str),
            },
        )

    def _emit_l3(
        self,
        ctx: RunContext,
        span_id: str,
        duration_ms: int,
        response: LLMResponse,
        *,
        template_id: str,
        action: str,
        confidence: float,
        extra: dict[str, Any],
    ) -> None:
        cost = response_cost(
            response.model, MODEL_ID, response.input_tokens, response.output_tokens,
            response.cache_read_input_tokens, rate_card=self.rate_card,
        )
        ctx.add_cost(cost)
        attrs: dict[str, Any] = {
            "shiftai.layer": "L3",
            "shiftai.decision.action_class": action,
            "shiftai.decision.confidence": confidence,
            "shiftai.decision.layer": 3,
            "shiftai.span.id": span_id,
            "shiftai.span.duration_ms": duration_ms,
            "gen_ai.request.model": MODEL_ID,
            "gen_ai.response.model": response.model,
            "gen_ai.usage.input_tokens": response.input_tokens,
            "gen_ai.usage.output_tokens": response.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": response.cache_read_input_tokens,
            "shiftai.model.version": response.model,
            "shiftai.prompt.template.id": template_id,
            "shiftai.prompt.template.version": gen.PROMPT_TEMPLATE_VERSION,
            "shiftai.prompt.system.version": SYSTEM_PROMPT_VERSION,
            **extra,
        }
        if cost is not None:
            attrs.update({
                "shiftai.cost.amount": cost,
                "shiftai.cost.currency": "USD",
                "shiftai.cost.model": "rate_card",
                "shiftai.cost.scope": "span_incremental",
            })
        self._emit(ctx, "decision_made", **attrs)

    def _cost_attrs(self, amount: float) -> dict[str, Any]:
        return {
            "shiftai.cost.amount": amount,
            "shiftai.cost.currency": "USD",
            "shiftai.cost.model": "rate_card",
            "shiftai.cost.scope": "run_total",
        }

    def _latency_attrs(self, ctx: RunContext) -> dict[str, Any]:
        lat = ctx.latency_breakdown_ms()
        return {
            "shiftai.run.id": ctx.run_id,
            "shiftai.latency.llm_ms": lat["llm"],
            "shiftai.latency.api_ms": lat["api"],
            "shiftai.latency.queue_ms": lat["queue"],
        }

    def _check_budget(self, ctx: RunContext) -> None:
        if ctx.latency_breakdown_ms()["total"] > RUN_TIMEOUT_S * 1000:
            raise TimeoutError(f"revision run exceeded {RUN_TIMEOUT_S}s budget")

    def _emit(self, ctx: RunContext, event_type: str, **attrs: Any) -> None:
        self.emitter.emit(
            event_type,
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            **{**ctx.run_attributes(), **attrs},
        )


def _elapsed_ms(since_iso: str) -> int:
    try:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(int((datetime.now(tz=UTC) - since).total_seconds() * 1000), 0)
