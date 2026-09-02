"""The Campaign-in-a-Box case state machine.

Planning pass (spec steps 1-8):
  approved brief → intel (SemRush + library, fallback flagged) → repository search
  → L3 pack (grounded, unsourced excluded) → L3 reuse/outlines (never create
  without search) → deterministic back-planning (gates never compressed) → kill
  switch/rate breaker → L4 workspace + docs + route pack/plan for confirmation.

Human gate (step 8): Marketing Lead confirms pack and plan (identity-stamped;
deltas applied as new versions). The orchestrator never confirms its own output
(guardrail 2 — no code path does).

Packaging module (steps 9-12, deterministic, NO LLM): completeness diff blocks on
any mismatch; naming auto-corrects only unambiguous cases; snapshots are planned
(read+hash, no writes) then committed; the manifest registers only after every
snapshot landed — partial manifests are impossible. Rework re-opens only affected
assets and re-hashes on re-entry; an unexplained hash change halts the package.

STS mapping (schema enum): every deterministic policy escalation carries
``shiftai.escalation.reason=policy_gap`` with the precise code in the additive
``shiftai.learn.reason_code``; L3 quality degradation maps to ``low_confidence``.
Human "confirmed" maps to hitl ``approved``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from shiftai_shared.brand import BrandRules
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store.store import ContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.llm import LLMProvider, LLMResponse
from shiftai_shared.resilience import IdempotencyStore, execute_idempotent
from shiftai_shared.telemetry import StsEmitter, TelemetrySink
from shiftai_shared.telemetry.envelope import RunContext, new_id, rate_card_cost

from c2c_campaign_box import (
    AGENT_TYPE,
    DATA_CLASSIFICATION,
    MODEL_ID,
    PACK_TEMPLATE_VERSION,
    PACKAGING_TIMEOUT_S,
    PLANNING_TIMEOUT_S,
    PROCESS_NAME,
    RISK_TIER,
    SYSTEM_PROMPT_VERSION,
)
from c2c_campaign_box import documents as docs
from c2c_campaign_box import persistence as db
from c2c_campaign_box import planning as planning_mod
from c2c_campaign_box.agent_config import OrchestratorConfig
from c2c_campaign_box.calendar import back_plan
from c2c_campaign_box.grounding import (
    ground_outlines,
    ground_pack,
    ground_reuse_items,
    valid_source_refs,
)
from c2c_campaign_box.intake import BriefNotApprovedError, load_approved_brief
from c2c_campaign_box.intel import IntelSource, gather_intel
from c2c_campaign_box.models import (
    AssetChecklist,
    AssetChecklistItem,
    AudienceOfferPack,
    CompletenessReport,
    ConfirmationRecord,
    ContentOutline,
    PackagedAsset,
    PackageManifest,
    PackagingOutcome,
    PlanOutcome,
    PlanStatus,
    RegisteredAsset,
    WorkflowPlan,
)
from c2c_campaign_box.packaging.completeness import (
    completeness_diff,
    missing_confirmation_records,
)
from c2c_campaign_box.packaging.naming import flagged_issues, validate_names
from c2c_campaign_box.packaging.snapshot import SnapshotReadError, plan_snapshots
from c2c_campaign_box.repository import RepositoryIndex, search_all_types
from c2c_campaign_box.workspace import (
    CampaignWorkspace,
    WorkspaceWriteError,
    campaign_folder_name,
    create_campaign_workspace,
    slugify,
)


class PlanGateError(Exception):
    """A gate violation: wrong state, unknown case, or a forbidden transition."""


class RunTimeoutError(Exception):
    """The per-run processing budget was exceeded (spec Timeout)."""


@dataclass
class OrchestratorDeps:
    provider: LLMProvider
    store: ContextStore
    workspace: CampaignWorkspace
    repository: RepositoryIndex
    intel_source: IntelSource | None  # None → intel-library-only fallback, flagged
    sink: TelemetrySink
    kill_switch: KillSwitch
    rate_breaker: RateBreaker
    idempotency: IdempotencyStore
    config: OrchestratorConfig
    settings: SharedSettings
    brand_rules: BrandRules


class CampaignBoxOrchestrator:
    def __init__(self, deps: OrchestratorDeps) -> None:
        self.deps = deps
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

    # ------------------------------------------------------------ planning pass

    def plan_campaign(
        self,
        campaign_id: str,
        *,
        trace_id: str | None = None,
        plan_date: date | None = None,
    ) -> PlanOutcome:
        """Steps 1-8. Triggered on brief approval (Execution Studio / dev bridge).
        Passing the intake trace_id keeps the whole campaign journey on one trace."""
        ctx = RunContext(case_id=campaign_id, trace_id=trace_id or new_id("trace"))
        today = plan_date or datetime.now(tz=UTC).date()
        self._emit(
            ctx,
            "case_intake",
            **{
                "shiftai.business_object.type": "campaign_brief",
                "shiftai.business_object.id": campaign_id,
            },
        )
        try:
            return self._planning_pipeline(ctx, campaign_id, today)
        except BriefNotApprovedError as exc:
            # Structured rejection (spec step 1) — recorded, never silently dropped.
            db.save_failed_run(self.deps.store, campaign_id, "BriefNotApprovedError", exc.detail)
            self._emit(ctx, "error", **{"error.type": "BriefNotApprovedError",
                                        "shiftai.outcome": "failure"})
            self._emit(ctx, "run_summary", **{"shiftai.outcome": "failure",
                                              "error.type": "BriefNotApprovedError",
                                              **self._latency_attrs(ctx)})
            return PlanOutcome(
                case_id=ctx.case_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="failed", escalation_reasons=["brief_not_approved"],
            )
        except Exception as exc:  # fail-closed: persist, emit, never discard
            return self._fail_planning(ctx, campaign_id, exc)

    def _planning_pipeline(self, ctx: RunContext, campaign_id: str, today: date) -> PlanOutcome:
        deps = self.deps
        config = deps.config
        escalations: list[str] = []
        self._emit(ctx, "config_loaded")

        # ---- Step 1: approved brief only -----------------------------------------
        brief = load_approved_brief(deps.store, campaign_id)

        # ---- Step 2: sourced intel (fallback flagged, never silent) ---------------
        with ctx.span("intel-gathering", "api") as intel_span:
            bundle = gather_intel(brief.topic, deps.workspace, deps.intel_source)
        self._emit(
            ctx,
            "tool_execution",
            **{
                "shiftai.layer": "L1",
                "gen_ai.tool.name": "intel.gather",
                "shiftai.span.id": intel_span.span_id,
                "shiftai.span.duration_ms": intel_span.duration_ms,
                "shiftai.intel.mode": bundle.mode,
                "shiftai.intel.signal_count": len(bundle.signals),
                "shiftai.intel.semrush_failure": bundle.semrush_failure,
            },
        )

        # ---- Step 5a: repository search (deterministic scores) --------------------
        with ctx.span("repository-search", "api") as repo_span:
            candidates_by_type, search_performed = search_all_types(
                deps.repository,
                config,
                business_unit=brief.field("business_unit"),
                vertical=brief.field("vertical"),
                topic=brief.topic,
            )
        self._emit(
            ctx,
            "tool_execution",
            **{
                "shiftai.layer": "L2",
                "gen_ai.tool.name": "repository.search",
                "shiftai.span.id": repo_span.span_id,
                "shiftai.span.duration_ms": repo_span.duration_ms,
                "shiftai.repository.search_performed": search_performed,
                "shiftai.repository.candidates": sum(len(v) for v in candidates_by_type.values()),
            },
        )
        self._emit(
            ctx,
            "policy_check",
            **{
                "shiftai.layer": "L2",
                "shiftai.policy.ids": ["approved_brief_only", "sourced_claims_only",
                                       "repository_read_only"],
                "shiftai.policy.decision": "allow",
                "shiftai.intel.mode": bundle.mode,
            },
        )

        # ---- Steps 3-4: L3 pack, grounded ----------------------------------------
        blocks = planning_mod.system_blocks(config, deps.brand_rules)
        with ctx.span("l3-pack-planning", "llm") as pack_span:
            pack_output, pack_response = planning_mod.run_pack_planning(
                deps.provider, blocks, brief, bundle
            )
        grounded, excluded, unverified_share, lint_findings = ground_pack(
            pack_output, bundle, brief, deps.brand_rules
        )
        self._emit_l3(
            ctx, pack_span.span_id, pack_span.duration_ms, pack_response,
            action="audience_offer_pack", confidence=pack_output.confidence,
            extra={
                "shiftai.pack.proof_points_verified": len(grounded.proof_points),
                "shiftai.pack.proof_points_excluded": len(excluded),
                "shiftai.pack.unverified_share": unverified_share,
                "shiftai.pack.lint_findings": len(lint_findings),
            },
        )
        self._check_budget(ctx, PLANNING_TIMEOUT_S)

        if unverified_share > config.thresholds.thin_intel_unverified_share:
            escalations.append("thin_intel")
            self._escalation_event(
                ctx, tier=2, reason_code="thin_intel",
                detail={"unverified_share": unverified_share,
                        "note": "thin intel base — human research needed"},
            )
        if excluded:
            # Recorded per spec guardrail 1 (excluded, flagged); telemetry only —
            # exclusion is by design, not an incident.
            self._emit(
                ctx,
                "tool_execution",
                **{
                    "shiftai.layer": "L3",
                    "gen_ai.tool.name": "grounding.exclude_unsourced",
                    "shiftai.span.duration_ms": 0,  # in-process check, no I/O
                    "shiftai.learn.reason_code": "unsourced_claim",
                    "shiftai.context_package": json.dumps(
                        [p.claim[:160] for p in excluded], ensure_ascii=False
                    ),
                },
            )

        pack_version = self._next_version(campaign_id, "pack_version")
        pack = AudienceOfferPack(
            campaign_id=campaign_id,
            version=pack_version,
            vertical=brief.field("vertical"),
            segment_applicability=grounded.segment_applicability,
            personas=grounded.personas,
            exclusions=grounded.exclusions,
            value_proposition=grounded.value_proposition,
            differentiators=grounded.differentiators,
            proof_points=grounded.proof_points,
            ctas=grounded.ctas,
            messaging_angles=grounded.messaging_angles,
            channel_emphasis=grounded.channel_emphasis,
            gaps=grounded.gaps,
            intel_mode=bundle.mode,
            unverified_share=unverified_share,
            lint_findings=lint_findings,
            template_version=PACK_TEMPLATE_VERSION,
            created_at=_now(),
        )

        # ---- Steps 5b-6: L3 reuse decisions + outlines ----------------------------
        skeleton = self._checklist_skeleton(campaign_id, candidates_by_type, search_performed)
        with ctx.span("l3-reuse-outlines", "llm") as reuse_span:
            reuse_output, reuse_response = planning_mod.run_reuse_outlines(
                deps.provider,
                blocks,
                brief,
                [i.model_dump(exclude={"candidates_evaluated"}) for i in skeleton],
                candidates_by_type,
                [a.model_dump() for a in pack.messaging_angles],
            )
        refs = valid_source_refs(bundle, brief)
        checklist_items = ground_reuse_items(
            reuse_output.items,
            candidates_by_type,
            skeleton,
            search_performed=search_performed,
            verified_refs=refs,
        )
        outlines = ground_outlines(reuse_output.items, checklist_items, refs)
        checklist = AssetChecklist(
            campaign_id=campaign_id,
            version=pack_version,
            items=checklist_items,
            search_performed=search_performed,
            created_at=_now(),
        )
        self._emit_l3(
            ctx, reuse_span.span_id, reuse_span.duration_ms, reuse_response,
            action="asset_checklist", confidence=reuse_output.confidence,
            extra={
                "shiftai.reuse.decisions": json.dumps(
                    {i.asset_id: i.decision for i in checklist_items}
                ),
                "shiftai.reuse.check_pending": sum(
                    1 for i in checklist_items if i.reuse_check_pending
                ),
                "shiftai.outlines.count": len(outlines),
            },
        )
        self._check_budget(ctx, PLANNING_TIMEOUT_S)

        # ---- Step 7: back-planned calendar (deterministic) ------------------------
        window_start, window_end = brief.window
        plan = back_plan(
            config,
            checklist_items,
            campaign_id=campaign_id,
            window_start=window_start,
            window_end=window_end,
            plan_date=today,
            existing_researched_blog_months=db.researched_blog_months(
                deps.store, exclude_campaign=campaign_id
            ),
            version=pack_version,
        )
        if not plan.feasible and plan.infeasibility is not None:
            escalations.append("infeasible_timeline")
            self._escalation_event(
                ctx, tier=2, reason_code="infeasible_timeline",
                detail={"reasons": plan.infeasibility.reasons,
                        "trade_offs": plan.infeasibility.trade_offs},
            )

        # ---- Control plane guard before Layer 4 side effects -----------------------
        kill_state, breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, config.agent_id,
            deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            self._escalation_event(
                ctx, tier=2, reason_code="tool_failure",
                detail={"control_pause_reason": pause_reason},
                control={"kill_switch": kill_state, "rate_breaker": breaker_state},
            )
            db.save_plan_case(
                deps.store, campaign_id,
                {"status": "escalated", "campaign_id": campaign_id,
                 "trace_id": ctx.trace_id, "escalations": [*escalations, "control_pause"],
                 "run_cost_usd": ctx.total_cost_usd},
            )
            return PlanOutcome(
                case_id=ctx.case_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="escalated", pack=pack, checklist=checklist, outlines=outlines,
                plan=plan, escalation_reasons=[*escalations, "control_pause"],
            )

        # ---- Step 7b: workspace from the versioned template + documents ------------
        folder = campaign_folder_name(config, brief.topic, window_start)
        campaign_slug = slugify(brief.topic)
        try:
            with ctx.span("workspace-create", "api") as ws_span:
                refs_map = create_campaign_workspace(deps.workspace, config, folder)
                pack_doc_ref = self._upload_once(
                    ctx, f"{campaign_id}:pack_doc:v{pack.version}", folder,
                    docs.pack_filename(pack), docs.pack_docx(pack, checklist),
                )
                self._upload_once(
                    ctx, f"{campaign_id}:pack_json:v{pack.version}", folder,
                    docs.pack_json_filename(pack), docs.pack_json(pack),
                )
                tracker_ref = self._upload_once(
                    ctx, f"{campaign_id}:tracker:v{plan.version}", folder,
                    docs.tracker_filename(plan), docs.tracker_csv(plan, checklist),
                )
        except WorkspaceWriteError as exc:
            self._escalation_event(
                ctx, tier=2, reason_code="workspace_failure",
                detail={"error": str(exc)}, routed_override=config.route_for("workspace_failure"),
            )
            raise
        self._emit(
            ctx,
            "tool_execution",
            **{
                "shiftai.layer": "L4",
                "gen_ai.tool.name": "workspace.create_campaign",
                "shiftai.span.id": ws_span.span_id,
                "shiftai.span.duration_ms": ws_span.duration_ms,
                "shiftai.workspace.folder": folder,
                "shiftai.workspace.refs": len(refs_map),
            },
        )

        # ---- Persist artifacts + register planned assets ---------------------------
        db.save_pack(deps.store, pack)
        db.save_checklist(deps.store, checklist)
        db.save_outlines(deps.store, campaign_id, [o.model_dump() for o in outlines])
        db.save_workflow_plan(deps.store, plan)
        entry_by_asset = {e.asset_id: e for e in plan.entries}
        for item in checklist_items:
            entry = entry_by_asset.get(item.asset_id)
            db.register_planned_asset(
                deps.store, campaign_id, item.asset_id,
                asset_type=item.asset_type,
                is_researched_blog=config.item_for(item.asset_type).is_researched_blog,
                draft_month=(entry.draft_due[:7] if entry else window_start[:7]),
            )

        # ---- Step 8: route pack + plan for Marketing Lead confirmation -------------
        routed_to = config.route_for("confirmation_pending")
        route_key = f"{campaign_id}:route_confirmation:v{pack.version}"

        def route_side_effect() -> dict[str, Any]:
            return {"task_id": f"confirm_{campaign_id}_v{pack.version}", "routed_to": routed_to}

        with ctx.span("l4-route-confirmation", "api") as action_span:
            result, was_repeat = execute_idempotent(
                route_key, deps.idempotency, route_side_effect
            )
        self._emit(
            ctx,
            "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "route_for_confirmation",
                "shiftai.action.idempotency_key": route_key,
                "shiftai.action.external_ref": pack_doc_ref,
                "shiftai.action.task_id": str(result["task_id"]),
                "shiftai.control.kill_switch": kill_state,
                "shiftai.control.rate_breaker": breaker_state,
                "shiftai.span.id": action_span.span_id,
                "shiftai.span.duration_ms": action_span.duration_ms,
                "shiftai.action.repeat": was_repeat,
            },
        )
        if not was_repeat:
            deps.rate_breaker.record_execution(config.agent_id)

        db.save_plan_case(
            deps.store, campaign_id,
            {
                "status": "awaiting_confirmation",
                "campaign_id": campaign_id,
                "trace_id": ctx.trace_id,
                "pack_version": pack.version,
                "plan_version": plan.version,
                "checklist_version": checklist.version,
                "folder": folder,
                "campaign_slug": campaign_slug,
                "pack_doc_ref": pack_doc_ref,
                "tracker_ref": tracker_ref,
                "confirmations": {"pack": False, "plan": False},
                "escalations": escalations,
                "reopened_assets": [],
                "manifest_version": 0,
                "awaiting_since": _now(),
                "run_cost_usd": ctx.total_cost_usd,
            },
        )
        self._emit(
            ctx, "run_summary",
            **{
                "shiftai.outcome": "partial" if escalations else "success",
                **self._latency_attrs(ctx),
                **(self._cost_attrs(ctx.total_cost_usd) if ctx.total_cost_usd > 0 else {}),
            },
        )
        return PlanOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            campaign_id=campaign_id,
            status="awaiting_confirmation",
            pack=pack,
            checklist=checklist,
            outlines=outlines,
            plan=plan,
            workspace_root=refs_map.get(folder),
            pack_doc_ref=pack_doc_ref,
            tracker_ref=tracker_ref,
            escalation_reasons=escalations,
        )

    # -------------------------------------------------------- confirmation gate

    def confirm(
        self,
        campaign_id: str,
        kind: Literal["pack", "plan"],
        *,
        decision: Literal["confirmed", "modified"],
        actor_id: str,
        actor_role: str = "marketing-lead",
        deltas: dict[str, Any] | None = None,
        notes: str | None = None,
    ) -> PlanOutcome:
        """Step 8 human gate. ``modified`` applies deltas as a new version and keeps
        the case awaiting; ``confirmed`` records the identity-stamped confirmation.
        Both confirmed → assets move to production. The orchestrator itself never
        calls this (guardrail 2)."""
        case = self._load_case_or_raise(campaign_id, expected={"awaiting_confirmation"})
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        deltas = deltas or {}
        if decision == "modified" and not deltas:
            raise PlanGateError("a modification needs at least one delta")
        record = ConfirmationRecord(
            kind=kind, decision=decision, actor_id=actor_id, actor_role=actor_role,
            timestamp=_now(), deltas=deltas, notes=notes,
        )
        db.save_confirmation(self.deps.store, campaign_id, record)
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "approved" if decision == "confirmed" else "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "none" if decision == "confirmed" else "pack_delta",
                "shiftai.learn.agent_recommendation": f"{kind}_proposal",
                "shiftai.learn.human_action": f"{decision}:{kind}",
                "shiftai.learn.decision_latency_ms": _elapsed_ms(
                    str(case.get("awaiting_since", ""))
                ),
            },
        )

        if decision == "modified":
            self._apply_deltas(ctx, case, kind, deltas)
            case = self._load_case_or_raise(campaign_id, expected={"awaiting_confirmation"})
            return self._outcome_from_case(ctx, case, status="awaiting_confirmation")

        confirmations = dict(case.get("confirmations", {}))
        confirmations[kind] = True
        new_status: PlanStatus = (
            "confirmed" if confirmations.get("pack") and confirmations.get("plan")
            else "awaiting_confirmation"
        )
        updated = {**case, "confirmations": confirmations, "status": new_status}
        if new_status == "confirmed":
            # Assets enter production; in production Agent 3 is signaled from here.
            checklist = self._load_checklist(campaign_id)
            items = [i.model_copy(update={"status": "in_production"}) for i in checklist.items]
            db.save_checklist(
                self.deps.store,
                checklist.model_copy(update={"items": items, "version": checklist.version + 1}),
            )
            updated["checklist_version"] = checklist.version + 1
            updated["status"] = "in_production"
            new_status = "in_production"
            self._emit(
                ctx, "case_resolved",
                **{
                    "shiftai.layer": "resolution",
                    "shiftai.outcome": "success",
                    "shiftai.resolution.outcome_source": "human",
                },
            )
        db.save_plan_case(self.deps.store, campaign_id, updated)
        return self._outcome_from_case(ctx, updated, status=new_status)

    def _apply_deltas(
        self, ctx: RunContext, case: dict[str, Any], kind: str, deltas: dict[str, Any]
    ) -> None:
        """Deltas produce a NEW version (append-only); nothing is edited in place."""
        store = self.deps.store
        campaign_id = str(case["campaign_id"])
        if kind == "pack":
            record = store.get(db.KIND_PACK, campaign_id)
            if record is None:
                raise PlanGateError("no pack exists to modify")
            pack = AudienceOfferPack.model_validate(record.value)
            allowed = {"value_proposition", "differentiators", "ctas", "exclusions",
                       "channel_emphasis", "segment_applicability"}
            updates = {k: v for k, v in deltas.items() if k in allowed}
            if not updates:
                raise PlanGateError(
                    f"no applicable pack deltas; allowed fields: {sorted(allowed)}"
                )
            new_pack = pack.model_copy(update={**updates, "version": pack.version + 1,
                                               "created_at": _now()})
            db.save_pack(store, new_pack)
            checklist = self._load_checklist(campaign_id)
            self._upload_once(
                ctx, f"{campaign_id}:pack_doc:v{new_pack.version}", str(case["folder"]),
                docs.pack_filename(new_pack), docs.pack_docx(new_pack, checklist),
            )
            db.save_plan_case(store, campaign_id, {**case, "pack_version": new_pack.version})
        else:
            record = store.get(db.KIND_WORKFLOW_PLAN, campaign_id)
            if record is None:
                raise PlanGateError("no workflow plan exists to modify")
            plan = WorkflowPlan.model_validate(record.value)
            allowed = {"window_start", "window_end"}
            updates = {k: str(v) for k, v in deltas.items() if k in allowed}
            if not updates:
                raise PlanGateError(
                    f"no applicable plan deltas; allowed fields: {sorted(allowed)}"
                )
            # A window change re-runs deterministic back-planning at full gate length.
            checklist = self._load_checklist(campaign_id)
            new_plan = back_plan(
                self.deps.config,
                checklist.items,
                campaign_id=campaign_id,
                window_start=updates.get("window_start", plan.window_start),
                window_end=updates.get("window_end", plan.window_end),
                plan_date=datetime.now(tz=UTC).date(),
                existing_researched_blog_months=db.researched_blog_months(
                    store, exclude_campaign=campaign_id
                ),
                version=plan.version + 1,
            )
            db.save_workflow_plan(store, new_plan)
            self._upload_once(
                ctx, f"{campaign_id}:tracker:v{new_plan.version}", str(case["folder"]),
                docs.tracker_filename(new_plan), docs.tracker_csv(new_plan, checklist),
            )
            db.save_plan_case(store, campaign_id, {**case, "plan_version": new_plan.version})

    # ------------------------------------- confirmed-asset intake (from Agent 4)

    def register_confirmed_asset(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        filename: str,
        content: bytes,
        actor_id: str,
        actor_role: str = "content-reviewer",
        claim_refs: list[str] | None = None,
    ) -> RegisteredAsset:
        """Spec input ``confirmed_assets``: a content-confirmed asset with its human
        confirmation record (production: Content Collaboration Agent; dev: bridge
        stand-in). The confirmation record is required — assets without one can
        never enter a package."""
        case = self._load_case_or_raise(
            campaign_id, expected={"in_production", "packaging_blocked"}
        )
        checklist = self._load_checklist(campaign_id)
        item = next((i for i in checklist.items if i.asset_id == asset_id), None)
        if item is None:
            raise PlanGateError(f"asset {asset_id!r} is not on the checklist")
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        prior = [a for a in db.load_registered_assets(self.deps.store, campaign_id)
                 if a.asset_id == asset_id]
        version = (max(a.version for a in prior) + 1) if prior else 1
        file_ref = self._upload_once(
            ctx, f"{campaign_id}:asset:{asset_id}:v{version}",
            f"{case['folder']}/drafts", filename, content,
        )
        record = ConfirmationRecord(
            kind="asset_content", decision="confirmed", actor_id=actor_id,
            actor_role=actor_role, timestamp=_now(), deltas={"version": version},
        )
        asset = RegisteredAsset(
            asset_id=asset_id,
            asset_type=item.asset_type,
            filename=filename,
            file_ref=file_ref,
            version=version,
            status="content_confirmed",
            confirmation=record,
            claim_refs=claim_refs or [],
        )
        db.register_asset(self.deps.store, campaign_id, asset)
        db.save_confirmation(self.deps.store, campaign_id, record)
        items = [
            i.model_copy(update={"status": "content_confirmed"}) if i.asset_id == asset_id else i
            for i in checklist.items
        ]
        db.save_checklist(self.deps.store, checklist.model_copy(update={"items": items}))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "approved",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "none",
                "shiftai.learn.agent_recommendation": "asset_content",
                "shiftai.learn.human_action": f"content_confirmed:{asset_id}",
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": asset_id,
            },
        )
        return asset

    # ------------------------------------------------------------ rework re-open

    def reopen_assets(
        self,
        campaign_id: str,
        asset_ids: list[str],
        *,
        requesting_gate: str,
        actor_id: str,
        actor_role: str = "quality-gate",
        notes: str | None = None,
    ) -> PlanOutcome:
        """Step 12: gate/approval returns re-open ONLY the affected assets."""
        if not asset_ids:
            raise PlanGateError("reopen requires at least one asset id")
        case = self._load_case_or_raise(
            campaign_id, expected={"packaged_pending_compliance", "packaging_blocked",
                                   "in_production"},
        )
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        checklist = self._load_checklist(campaign_id)
        known = {i.asset_id for i in checklist.items}
        unknown = [a for a in asset_ids if a not in known]
        if unknown:
            raise PlanGateError(f"unknown assets in reopen request: {unknown}")
        items = [
            i.model_copy(update={"status": "in_production"}) if i.asset_id in asset_ids else i
            for i in checklist.items
        ]
        db.save_checklist(self.deps.store, checklist.model_copy(update={"items": items}))
        reopened = sorted(set(case.get("reopened_assets", [])) | set(asset_ids))
        db.save_plan_case(
            self.deps.store, campaign_id,
            {**case, "status": "in_production", "reopened_assets": reopened,
             "reopen_gate": requesting_gate},
        )
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "rework_reopen",
                "shiftai.learn.agent_recommendation": "packaged_pending_compliance",
                "shiftai.learn.human_action": f"reopen:{','.join(sorted(asset_ids))}",
                "shiftai.context_package": json.dumps(
                    {"requesting_gate": requesting_gate, "notes": notes}
                ),
            },
        )
        return self._outcome_from_case(ctx, {**case, "status": "in_production"},
                                       status="in_production")

    # --------------------------------------------------------- packaging module

    def run_packaging(self, campaign_id: str) -> PackagingOutcome:
        """Steps 9-11 (deterministic; NO LLM). Transactional: the manifest registers
        only after every snapshot landed; any block produces an actionable report."""
        case = self._load_case_or_raise(
            campaign_id, expected={"in_production", "packaging_blocked"}
        )
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        deps = self.deps
        config = deps.config

        kill_state, breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, config.agent_id,
            deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            self._escalation_event(
                ctx, tier=2, reason_code="tool_failure",
                detail={"control_pause_reason": pause_reason},
                control={"kill_switch": kill_state, "rate_breaker": breaker_state},
            )
            return PackagingOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="in_production", escalation_reasons=["control_pause"],
            )

        checklist = self._load_checklist(campaign_id)
        registered = db.load_registered_assets(deps.store, campaign_id)

        # Latest confirmed version per asset only.
        latest: dict[str, RegisteredAsset] = {}
        for asset in registered:
            current = latest.get(asset.asset_id)
            if current is None or asset.version > current.version:
                latest[asset.asset_id] = asset
        current_assets = list(latest.values())

        # ---- Step 9: completeness + confirmation records ---------------------------
        diff = completeness_diff(checklist.items, current_assets)
        missing_conf = missing_confirmation_records(current_assets)
        campaign_slug = str(case.get("campaign_slug", slugify(campaign_id)))
        canonical_names, naming_issues = validate_names(config, campaign_slug, current_assets)
        naming_blocks = flagged_issues(naming_issues)
        self._emit(
            ctx,
            "policy_check",
            **{
                "shiftai.layer": "L2",
                "shiftai.policy.ids": ["completeness_diff", "confirmation_records",
                                       "naming_convention"],
                "shiftai.policy.decision": (
                    "escalate" if (not diff.empty or missing_conf or naming_blocks) else "allow"
                ),
                "shiftai.packaging.diff_missing": len(diff.missing),
                "shiftai.packaging.diff_extra": len(diff.extra),
                "shiftai.packaging.diff_version_mismatch": len(diff.version_mismatch),
                "shiftai.packaging.missing_confirmations": len(missing_conf),
                "shiftai.packaging.naming_flags": len(naming_blocks),
            },
        )
        if not diff.empty or missing_conf or naming_blocks:
            report = CompletenessReport(
                campaign_id=campaign_id,
                diff=diff,
                missing_confirmations=missing_conf,
                naming_flags=naming_issues,
                owners_note=(
                    "packaging blocked — resolve missing/extra/mismatched assets with the "
                    "asset owners; the diff is never padded or trimmed to fit"
                ),
                created_at=_now(),
            )
            db.save_completeness_report(deps.store, report)
            reasons = []
            if not diff.empty:
                reasons.append("completeness_block")
            if missing_conf:
                reasons.append("missing_confirmation_record")
            if naming_blocks:
                reasons.append("naming_ambiguous")
            for code in reasons:
                self._escalation_event(
                    ctx, tier=2, reason_code=code,
                    detail={"diff": diff.model_dump(),
                            "missing_confirmations": missing_conf,
                            "naming_flags": [n.model_dump() for n in naming_blocks]},
                )
            db.save_plan_case(deps.store, campaign_id, {**case, "status": "packaging_blocked"})
            return PackagingOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="packaging_blocked", report=report, escalation_reasons=reasons,
            )

        # ---- Step 10: plan snapshots (pure read + hash — no writes yet) -------------
        try:
            with ctx.span("snapshot-plan", "api"):
                snapshot_plan = plan_snapshots(deps.workspace, current_assets, canonical_names)
        except SnapshotReadError as exc:
            self._escalation_event(
                ctx, tier=3, reason_code="workspace_failure", detail={"error": str(exc)},
                routed_override=config.route_for("workspace_failure"),
            )
            db.save_failed_run(deps.store, campaign_id, "SnapshotReadError", str(exc))
            return PackagingOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status=str(case["status"]),  # type: ignore[arg-type]
                escalation_reasons=["workspace_failure"],
            )

        # Re-entry re-hash: a previously packaged asset whose bytes changed WITHOUT a
        # rework re-open is a hash mismatch → halt, escalate to AiCoE.
        manifest_record = deps.store.get(db.KIND_MANIFEST, campaign_id)
        reopened = set(case.get("reopened_assets", []))
        if manifest_record is not None:
            prior = PackageManifest.model_validate(manifest_record.value)
            prior_hashes = {a.asset_id: a.sha256 for a in prior.assets}
            mismatched = [
                item.asset_id
                for item in snapshot_plan
                if item.asset_id in prior_hashes
                and item.asset_id not in reopened
                and prior_hashes[item.asset_id] != item.sha256
            ]
            if mismatched:
                self._escalation_event(
                    ctx, tier=3, reason_code="hash_mismatch",
                    detail={"assets": mismatched,
                            "note": "post-packaging edit detected without a rework re-open"},
                    routed_override=config.route_for("hash_mismatch"),
                )
                db.save_plan_case(deps.store, campaign_id,
                                  {**case, "status": "packaging_blocked"})
                return PackagingOutcome(
                    case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                    status="packaging_blocked", escalation_reasons=["hash_mismatch"],
                )

        # ---- Step 11: commit snapshots, then register the manifest ------------------
        manifest_version = int(case.get("manifest_version", 0)) + 1
        packaged: list[PackagedAsset] = []
        committed_refs: list[str] = []
        try:
            with ctx.span("snapshot-commit", "api") as commit_span:
                for item in snapshot_plan:
                    # Content-addressed key: an unchanged asset re-uses the snapshot
                    # it already landed in a prior manifest run (no duplicate write);
                    # changed content gets a new canonical name + fresh upload.
                    key = (
                        f"{campaign_id}:snapshot:{item.canonical_name}:{item.sha256[:16]}"
                    )

                    def upload(item: Any = item) -> dict[str, Any]:
                        ref = deps.workspace.upload(
                            f"{case['folder']}/final", item.canonical_name, item.content
                        )
                        return {"ref": ref}

                    result, _ = execute_idempotent(key, deps.idempotency, upload)
                    committed_refs.append(str(result["ref"]))
                    packaged.append(
                        PackagedAsset(
                            asset_id=item.asset_id,
                            asset_type=item.asset_type,
                            canonical_name=item.canonical_name,
                            source_ref=item.source_ref,
                            snapshot_ref=str(result["ref"]),
                            version=item.version,
                            sha256=item.sha256,
                        )
                    )
        except Exception as exc:
            # Transactional revert: no manifest registers; state returns to
            # in_production; landed snapshot refs are recorded for the retry (the
            # idempotency store resumes them — never a partial manifest).
            db.save_failed_run(
                deps.store, campaign_id, "PackagingCommitError",
                json.dumps({"error": str(exc), "committed_refs": committed_refs}),
            )
            db.save_plan_case(deps.store, campaign_id, {**case, "status": "in_production"})
            self._emit(ctx, "error", **{"error.type": type(exc).__name__,
                                        "shiftai.outcome": "failure"})
            self._escalation_event(
                ctx, tier=3, reason_code="workspace_failure",
                detail={"error": str(exc), "committed_refs": committed_refs},
                routed_override=config.route_for("workspace_failure"),
            )
            return PackagingOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="in_production", escalation_reasons=["workspace_failure"],
            )

        claim_lineage = {a.asset_id: a.claim_refs for a in current_assets if a.claim_refs}
        manifest = PackageManifest(
            manifest_id=new_id("manifest"),
            campaign_id=campaign_id,
            version=manifest_version,
            assets=packaged,
            calendar_ref=str(case.get("tracker_ref", "")),
            checklist_version=checklist.version,
            claim_lineage_index=claim_lineage,
            naming_corrections=naming_issues,
            created_at=_now(),
        )
        manifest_key = f"{campaign_id}:manifest:v{manifest_version}"

        def register() -> dict[str, Any]:
            db.save_manifest(deps.store, manifest)
            return {"manifest_id": manifest.manifest_id}

        result, was_repeat = execute_idempotent(manifest_key, deps.idempotency, register)
        self._emit(
            ctx,
            "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "register_package_manifest",
                "shiftai.action.idempotency_key": manifest_key,
                "shiftai.action.external_ref": str(result["manifest_id"]),
                "shiftai.control.kill_switch": kill_state,
                "shiftai.control.rate_breaker": breaker_state,
                "shiftai.span.id": commit_span.span_id,
                "shiftai.span.duration_ms": commit_span.duration_ms,
                "shiftai.action.repeat": was_repeat,
                "shiftai.packaging.assets": len(packaged),
                "shiftai.packaging.naming_auto_corrected": sum(
                    1 for n in naming_issues if n.resolution == "auto_corrected"
                ),
            },
        )
        if not was_repeat:
            deps.rate_breaker.record_execution(config.agent_id)

        # Packaged statuses + packaging summary to the status tracker (new version).
        items = [i.model_copy(update={"status": "packaged"}) for i in checklist.items]
        new_checklist = checklist.model_copy(
            update={"items": items, "version": checklist.version + 1}
        )
        db.save_checklist(deps.store, new_checklist)
        plan_record = deps.store.get(db.KIND_WORKFLOW_PLAN, campaign_id)
        tracker_ref = str(case.get("tracker_ref", ""))
        if plan_record is not None:
            plan = WorkflowPlan.model_validate(plan_record.value)
            summary_plan = plan.model_copy(update={"version": plan.version + manifest_version})
            tracker_ref = self._upload_once(
                ctx, f"{campaign_id}:tracker:v{summary_plan.version}", str(case["folder"]),
                docs.tracker_filename(summary_plan), docs.tracker_csv(summary_plan, new_checklist),
            )
            db.save_workflow_plan(deps.store, summary_plan)
        db.save_plan_case(
            deps.store, campaign_id,
            {**case, "status": "packaged_pending_compliance",
             "manifest_version": manifest_version, "reopened_assets": [],
             "tracker_ref": tracker_ref},
        )
        lat = ctx.latency_breakdown_ms()
        if lat["total"] > PACKAGING_TIMEOUT_S * 1000:
            raise RunTimeoutError(f"packaging exceeded {PACKAGING_TIMEOUT_S}s budget")
        self._emit(
            ctx, "run_summary",
            **{"shiftai.outcome": "success", **self._latency_attrs(ctx)},
        )
        return PackagingOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status="packaged_pending_compliance", manifest=manifest,
        )

    # ------------------------------------------------------------------ helpers

    def _checklist_skeleton(
        self,
        campaign_id: str,
        candidates_by_type: dict[str, list[Any]],
        search_performed: bool,
    ) -> list[AssetChecklistItem]:
        items: list[AssetChecklistItem] = []
        for comp in self.deps.config.composition:
            if not comp.required:
                continue
            items.append(
                AssetChecklistItem(
                    asset_id=comp.asset_type,
                    asset_type=comp.asset_type,
                    label=comp.label,
                    volume=comp.volume_cap,
                    decision="create",
                    decision_rationale="pending planning pass",
                    candidates_evaluated=candidates_by_type.get(comp.asset_type, []),
                    reuse_check_pending=not search_performed,
                )
            )
        return items

    def _upload_once(
        self, ctx: RunContext, key: str, folder: str, filename: str, content: bytes
    ) -> str:
        def side_effect() -> dict[str, Any]:
            ref = self.deps.workspace.upload(folder, filename, content)
            return {"ref": ref}

        result, _ = execute_idempotent(key, self.deps.idempotency, side_effect)
        return str(result["ref"])

    def _load_checklist(self, campaign_id: str) -> AssetChecklist:
        record = self.deps.store.get(db.KIND_CHECKLIST, campaign_id)
        if record is None:
            raise PlanGateError(f"no asset checklist exists for {campaign_id!r}")
        return AssetChecklist.model_validate(record.value)

    def _next_version(self, campaign_id: str, field: str) -> int:
        case = db.load_plan_case(self.deps.store, campaign_id) or {}
        return int(case.get(field, 0)) + 1

    def _outcome_from_case(
        self, ctx: RunContext, case: dict[str, Any], *, status: PlanStatus
    ) -> PlanOutcome:
        store = self.deps.store
        campaign_id = str(case["campaign_id"])
        pack_record = store.get(db.KIND_PACK, campaign_id)
        plan_record = store.get(db.KIND_WORKFLOW_PLAN, campaign_id)
        checklist_record = store.get(db.KIND_CHECKLIST, campaign_id)
        outlines_record = store.get(db.KIND_OUTLINES, campaign_id)
        outlines: list[ContentOutline] = []
        if outlines_record is not None:
            outlines = [
                ContentOutline.model_validate(o)
                for o in outlines_record.value.get("outlines", [])
            ]
        return PlanOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            campaign_id=campaign_id,
            status=status,
            pack=AudienceOfferPack.model_validate(pack_record.value) if pack_record else None,
            checklist=(
                AssetChecklist.model_validate(checklist_record.value)
                if checklist_record else None
            ),
            outlines=outlines,
            plan=WorkflowPlan.model_validate(plan_record.value) if plan_record else None,
            pack_doc_ref=str(case.get("pack_doc_ref")) if case.get("pack_doc_ref") else None,
            tracker_ref=str(case.get("tracker_ref")) if case.get("tracker_ref") else None,
            escalation_reasons=list(case.get("escalations", [])),
        )

    def _fail_planning(self, ctx: RunContext, campaign_id: str, exc: Exception) -> PlanOutcome:
        error_type = type(exc).__name__
        try:
            db.save_failed_run(self.deps.store, campaign_id, error_type, str(exc))
            db.save_plan_case(
                self.deps.store, campaign_id,
                {"status": "failed", "campaign_id": campaign_id, "trace_id": ctx.trace_id,
                 "error_type": error_type},
            )
        except Exception:
            pass  # store down: telemetry below is the surviving record
        self._emit(ctx, "error", **{"error.type": error_type, "shiftai.outcome": "failure"})
        self._emit(
            ctx, "run_summary",
            **{"shiftai.outcome": "failure", "error.type": error_type,
               **self._latency_attrs(ctx)},
        )
        return PlanOutcome(
            case_id=ctx.case_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status="failed", escalation_reasons=[error_type],
        )

    def _escalation_event(
        self,
        ctx: RunContext,
        *,
        tier: int,
        reason_code: str,
        detail: dict[str, Any],
        control: dict[str, str] | None = None,
        routed_override: str | None = None,
    ) -> None:
        attrs: dict[str, Any] = {
            "shiftai.layer": "escalation",
            "shiftai.escalation.tier": tier,
            "shiftai.escalation.reason": "policy_gap",
            "shiftai.escalation.routed_to": (
                routed_override or self.deps.config.route_for(reason_code)
            ),
            "shiftai.learn.reason_code": reason_code,
            "shiftai.context_package": json.dumps(detail, default=str),
        }
        if control:
            attrs["shiftai.control.kill_switch"] = control["kill_switch"]
            attrs["shiftai.control.rate_breaker"] = control["rate_breaker"]
        self._emit(ctx, "case_escalated", **attrs)

    def _emit_l3(
        self,
        ctx: RunContext,
        span_id: str,
        duration_ms: int,
        response: LLMResponse,
        *,
        action: str,
        confidence: float,
        extra: dict[str, Any],
    ) -> None:
        cost = rate_card_cost(
            MODEL_ID, response.input_tokens, response.output_tokens,
            response.cache_read_input_tokens,
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
            "shiftai.prompt.template.id": planning_mod.PROMPT_TEMPLATE_ID,
            "shiftai.prompt.template.version": planning_mod.PROMPT_TEMPLATE_VERSION,
            "shiftai.prompt.system.version": SYSTEM_PROMPT_VERSION,
            **extra,
        }
        if cost is not None:
            attrs.update(
                {
                    "shiftai.cost.amount": cost,
                    "shiftai.cost.currency": "USD",
                    "shiftai.cost.model": "rate_card",
                    "shiftai.cost.scope": "span_incremental",
                }
            )
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

    def _load_case_or_raise(self, campaign_id: str, expected: set[str]) -> dict[str, Any]:
        case = db.load_plan_case(self.deps.store, campaign_id)
        if case is None:
            raise PlanGateError(f"unknown campaign plan {campaign_id!r}")
        if case.get("status") not in expected:
            raise PlanGateError(
                f"campaign {campaign_id!r} is {case.get('status')!r}; "
                f"expected one of {sorted(expected)}"
            )
        return case

    def _check_budget(self, ctx: RunContext, budget_s: float) -> None:
        if ctx.latency_breakdown_ms()["total"] > budget_s * 1000:
            raise RunTimeoutError(f"processing exceeded {budget_s}s budget")

    def _emit(self, ctx: RunContext, event_type: str, **attrs: Any) -> None:
        self.emitter.emit(
            event_type,
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            **{**ctx.run_attributes(), **attrs},
        )


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _elapsed_ms(since_iso: str) -> int:
    try:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(int((datetime.now(tz=UTC) - since).total_seconds() * 1000), 0)
