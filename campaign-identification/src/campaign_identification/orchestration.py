"""The Campaign Identification case state machine (kit orchestration mapped to Agent 1):

intake → L1 normalize → L2 policy (validation, BC/F&O, duplicates, compliance ceiling)
→ L3 Sonnet classification (only when L2 finds no blocking match) → authority envelope
→ kill switch → rate breaker → L4 (Word brief + approval routing, idempotent) or
escalation → human gate (BU Campaign Lead) → resolution.

Every step emits an STS v2 record (the audit trail); the sequence for one case
follows telemetry-standard.md §9. Escalation-reason mapping (documented in
agent-spec.md §6): deterministic L2 escalations → ``policy_gap``; L3 abstention /
low confidence / unclassifiable → ``low_confidence``; the precise routing-map
uncertainty type rides in the additive attribute ``shiftai.escalation.uncertainty_type``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shiftai_shared.business_capability import DecisionAgentConfig
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store.store import ContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.llm import LLMProvider, LLMResponse
from shiftai_shared.prompting import PROMPT_TEMPLATE_ID, PROMPT_TEMPLATE_VERSION
from shiftai_shared.resilience import IdempotencyStore, execute_idempotent
from shiftai_shared.telemetry import StsEmitter, TelemetrySink
from shiftai_shared.telemetry.envelope import RunContext, new_id, rate_card_cost

from campaign_identification import (
    AGENT_TYPE,
    CONFIDENCE_THRESHOLD,
    DATA_CLASSIFICATION,
    MAX_GAP_ROUNDS,
    MODEL_ID,
    PROCESS_NAME,
    RISK_TIER,
    RUN_TIMEOUT_S,
    SYSTEM_PROMPT_VERSION,
)
from campaign_identification import brief as brief_mod
from campaign_identification import persistence as db
from campaign_identification.approval import (
    ApprovalGateError,
    learning_label,
    scenario_hash,
)
from campaign_identification.approval import (
    record as record_approval,
)
from campaign_identification.classify import (
    build_case_data,
    derive_priority,
    run_classification,
    system_blocks,
)
from campaign_identification.conflicts import blocking_duplicates, detect_conflicts
from campaign_identification.extraction import EXTRACTABLE_FIELDS, extract_fields
from campaign_identification.gaps import draft_gap_request, gap_reason_codes
from campaign_identification.intake import merge_gap_answers, normalize_request
from campaign_identification.models import (
    BcFoCheck,
    CampaignBrief,
    CampaignRequest,
    CaseStatus,
    ClassifyOutput,
    ConflictFlag,
    GapRequest,
    IntakeContext,
    ProcessOutcome,
    RequestSource,
    ValidationResult,
)
from campaign_identification.revision import revise_request_fields
from campaign_identification.rules import check_bc_fo, touches_compliance
from campaign_identification.validation import validate_request


class RunTimeoutError(Exception):
    """The 120s per-run processing budget was exceeded (spec Timeout)."""


@dataclass
class AgentDeps:
    provider: LLMProvider
    store: ContextStore
    workspace: db.Workspace
    sink: TelemetrySink
    kill_switch: KillSwitch
    rate_breaker: RateBreaker
    idempotency: IdempotencyStore
    config: DecisionAgentConfig
    settings: SharedSettings


class CampaignIdentificationAgent:
    def __init__(self, deps: AgentDeps) -> None:
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

    # ------------------------------------------------------------------ intake

    def process_request(
        self,
        raw: dict[str, Any],
        source: RequestSource,
        *,
        source_ref: str | None = None,
        trace_id: str | None = None,
        hold_for_verification: bool = False,
    ) -> ProcessOutcome:
        """Entry point for a new request from any of the three entry points.

        ``hold_for_verification=True`` keeps the drafted brief with the requester
        (status ``draft_review``) instead of routing it — the AI-first intake flow:
        the requester verifies/iterates, then ``release_brief`` routes it.
        """
        case_id = new_id("case")
        trace_id = trace_id or new_id("trace")
        request = normalize_request(raw, source, source_ref=source_ref)
        ctx = RunContext(case_id=case_id, trace_id=trace_id)
        self._emit(
            ctx,
            "case_intake",
            **{
                "shiftai.business_object.type": "campaign_request",
                "shiftai.business_object.id": request.request_id,
                "shiftai.request.source": source,
            },
        )
        try:
            return self._pipeline(ctx, request, raw, gap_round=0, hold=hold_for_verification)
        except Exception as exc:  # fail-closed: persist, emit, never discard
            return self._fail(ctx, raw, request, exc)

    def submit_gap_answers(
        self,
        case_id: str,
        answers: dict[str, Any],
        *,
        actor_role: str = "requester",
        actor_id: str,
        release_after: bool = False,
    ) -> ProcessOutcome:
        """Requester answers to a gap request (human input → human_gate 'modified',
        then the case resumes in the same trace). Also accepts field edits while the
        draft is held in review. ``release_after`` routes the brief immediately when
        the resumed run lands in ``draft_review`` (the requester's send = verification).
        """
        case = self._load_case_or_raise(case_id, expected={"awaiting_input", "draft_review"})
        approval = record_approval(decision="modified", actor_role=actor_role, actor_id=actor_id)
        request = db.request_from_case(case)
        ctx = RunContext(case_id=case_id, trace_id=str(case["trace_id"]))
        s_hash = scenario_hash(None, str(case.get("action_class")))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "missing_field",
                "shiftai.learn.agent_recommendation": case.get("action_class"),
                "shiftai.learn.human_action": "provided_gap_answers",
                "shiftai.learn.scenario_hash": s_hash,
            },
        )
        db.save_human_decision(self.deps.store, case_id, approval, s_hash)
        merged = merge_gap_answers(request, answers)
        hold = bool(case.get("hold_for_verification", False))
        try:
            outcome = self._pipeline(
                ctx,
                merged,
                merged.model_dump(),
                gap_round=int(case.get("gap_rounds", 0)),
                hold=hold,
            )
        except Exception as exc:
            return self._fail(ctx, merged.model_dump(), merged, exc)
        if release_after and outcome.status == "draft_review":
            return self.release_brief(case_id, actor_id=actor_id, actor_role=actor_role)
        return outcome

    # ------------------------------------------------------------ the pipeline

    def _pipeline(
        self,
        ctx: RunContext,
        request: CampaignRequest,
        raw: dict[str, Any],
        *,
        gap_round: int,
        hold: bool = False,
    ) -> ProcessOutcome:
        deps = self.deps
        config = deps.config
        self._emit(ctx, "config_loaded")

        # ---- Layer 1: extraction from the requester's own words -----------------
        # Fills empty extractable fields from free_text_context with quoted
        # provenance; segment/budget/dates/owner are never extracted (they stay
        # with the human). Best-effort — a failure leaves the gap flow in charge.
        needs_extraction = bool(request.free_text_context) and any(
            getattr(request, f) in (None, "", []) for f in EXTRACTABLE_FIELDS
        )
        if needs_extraction:
            with ctx.span("l1-extraction", "llm") as extract_span:
                request, extraction_response = extract_fields(
                    deps.provider, system_blocks(config), request
                )
            if extraction_response is not None:
                cost = rate_card_cost(
                    MODEL_ID,
                    extraction_response.input_tokens,
                    extraction_response.output_tokens,
                    extraction_response.cache_read_input_tokens,
                )
                ctx.add_cost(cost)
                attrs: dict[str, Any] = {
                    "shiftai.layer": "L1",
                    "gen_ai.tool.name": "layer1.extract_fields",
                    "shiftai.span.id": extract_span.span_id,
                    "shiftai.span.duration_ms": extract_span.duration_ms,
                    "shiftai.extraction.derived_fields": sorted(request.derived_fields),
                    **self._llm_attributes(extraction_response),
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
                self._emit(ctx, "tool_execution", **attrs)

        # ---- Layer 2: deterministic policy pass ---------------------------------
        with ctx.span("l2-validation", "other"):
            validation = validate_request(request, config.intake_schema)
            bc_fo = check_bc_fo(request)
            calendar = db.load_calendar(deps.store)
            conflicts = detect_conflicts(request, calendar, decay_days=config.precedent_decay_days)
            compliance_hits = touches_compliance(request)
        hard_duplicates = blocking_duplicates(conflicts)
        fired: list[str] = []
        if not validation.complete:
            fired.append("missing_mandatory_fields")
        if bc_fo.mixed:
            fired.append("bc_fo_mixed")
        if hard_duplicates:
            fired.append("fresh_duplicate")
        if compliance_hits:
            fired.append("authority-envelope.compliance-ceiling")
        self._emit(
            ctx,
            "policy_check",
            **{
                "shiftai.layer": "L2",
                "shiftai.policy.ids": fired or [r.id for r in config.policy_rules],
                "shiftai.policy.decision": "escalate" if fired else "allow",
                "shiftai.intake.completeness_score": validation.completeness_score,
                "shiftai.intake.duplicate_flags": len(conflicts),
                "shiftai.intake.gap_round": gap_round,
            },
        )
        self._check_budget(ctx)

        # ---- L2 decisions (config policy-rule order; first match wins) ----------
        if not validation.complete:
            return self._handle_gaps(ctx, request, validation, gap_round, hold=hold)
        if bc_fo.mixed:
            return self._escalate(
                ctx,
                request,
                action_class="flag_bc_fo_mix",
                decided_by_layer=2,
                tier=2,
                reason="policy_gap",
                uncertainty="policy_gap",
                routed_to=config.route_for("policy_gap"),
                reason_code="bc_fo_mix",
                detail={"evidence": bc_fo.evidence, "split_proposal": bc_fo.split_proposal},
                validation=validation,
                conflicts=conflicts,
            )
        if hard_duplicates:
            return self._escalate(
                ctx,
                request,
                action_class="flag_duplicate",
                decided_by_layer=2,
                tier=2,
                reason="policy_gap",
                uncertainty="policy_gap",
                routed_to=config.route_for("policy_gap"),
                reason_code="duplicate_disputed",
                detail={
                    "conflicting_campaign_ids": [c.conflicting_campaign_id for c in hard_duplicates]
                },
                validation=validation,
                conflicts=conflicts,
            )
        if compliance_hits:
            return self._escalate(
                ctx,
                request,
                action_class=None,
                decided_by_layer=2,
                tier=3,
                reason="policy_gap",
                uncertainty="policy_gap",
                routed_to=config.route_for("policy_gap"),
                reason_code="compliance_ceiling",
                detail={"matched_terms": compliance_hits},
                validation=validation,
                conflicts=conflicts,
            )

        # ---- Layer 3: reasoning (only when L2 found no blocking match) ----------
        plan_linked = request.source == "plan" or any("plan" in ref for ref in request.source_refs)
        case_data = build_case_data(
            request, validation, conflicts, bc_fo, derive_priority(request, plan_linked)
        )
        with ctx.span("l3-classification", "llm"):
            output, response, _ = run_classification(deps.provider, config, case_data, request)
        self._emit_l3_decision(ctx, output, response)
        self._check_budget(ctx)

        if (
            output.action_class in (None, "escalate_unclassifiable")
            or output.confidence < CONFIDENCE_THRESHOLD
            or output.classification is None
        ):
            return self._escalate(
                ctx,
                request,
                action_class=output.action_class,
                decided_by_layer=3,
                tier=2,
                reason="low_confidence",
                uncertainty="confidence_only",
                routed_to=config.route_for("policy_gap"),
                reason_code="unclassifiable_bu",
                detail={"rationale": output.rationale, "confidence": output.confidence},
                validation=validation,
                conflicts=conflicts,
                already_decided=True,  # decision_made(L3) was just emitted
            )

        # ---- Control plane guard before any Layer 4 action -----------------------
        kill_state, breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, config.agent_id, deps.settings.shiftai_tenant_id
        )
        if kill_state == "paused":
            return self._escalate(
                ctx,
                request,
                action_class=output.action_class,
                decided_by_layer=3,
                tier=2,
                reason="policy_gap",
                uncertainty="policy_gap",
                routed_to=config.route_for("policy_gap"),
                reason_code="tool_failure" if breaker_state == "tripped" else "sla_breach",
                detail={"control_pause_reason": pause_reason},
                validation=validation,
                conflicts=conflicts,
                control={"kill_switch": kill_state, "rate_breaker": breaker_state},
                already_decided=True,  # decision_made(L3) was just emitted
            )

        # ---- Layer 4: assemble brief, write to workspace ---------------------------
        case = db.load_case(deps.store, ctx.case_id) or {}
        version = int(case.get("brief_version", 0)) + 1
        campaign_brief = brief_mod.assemble_brief(
            case_id=ctx.case_id,
            request=request,
            classification=output.classification,
            conflicts=conflicts,
            bc_fo=bc_fo,
            normalized_fields=output.normalized_fields,
            version=version,
            campaign_id=case.get("campaign_id"),
        )
        doc_ref = self._upload_brief(ctx, campaign_brief)
        status: CaseStatus = "draft_review" if hold else "awaiting_approval"
        db.save_case(
            deps.store,
            ctx.case_id,
            {
                "status": status,
                "request": request.model_dump(),
                "raw_request": raw,
                "trace_id": ctx.trace_id,
                "gap_rounds": gap_round,
                "brief_version": version,
                "campaign_id": campaign_brief.campaign_id,
                "action_class": "route_for_approval",
                "brief": campaign_brief.model_dump(),
                "doc_ref": doc_ref,
                "hold_for_verification": hold,
                "awaiting_since": _now(),
                "run_cost_usd": ctx.total_cost_usd + float(case.get("run_cost_usd", 0.0)),
            },
        )
        if hold:
            # Draft stays with the requester — no routing action fires until
            # release_brief records their explicit verification.
            self._save_context(
                ctx, request, "draft_review", output, validation, conflicts, gap_round
            )
            return ProcessOutcome(
                case_id=ctx.case_id,
                trace_id=ctx.trace_id,
                status="draft_review",
                action_class="route_for_approval",
                brief=campaign_brief,
                doc_ref=doc_ref,
            )
        self._route_for_approval(
            ctx,
            campaign_brief,
            doc_ref,
            version,
            control={"kill_switch": kill_state, "rate_breaker": breaker_state},
        )
        self._save_context(
            ctx, request, "awaiting_approval", output, validation, conflicts, gap_round
        )
        return ProcessOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            status="awaiting_approval",
            action_class="route_for_approval",
            brief=campaign_brief,
            doc_ref=doc_ref,
        )

    def _upload_brief(self, ctx: RunContext, campaign_brief: CampaignBrief) -> str:
        """Idempotent workspace write of one brief version (no LLM)."""
        key = f"{ctx.case_id}:draft_brief:v{campaign_brief.version}"

        def side_effect() -> dict[str, Any]:
            docx_bytes = brief_mod.brief_docx(campaign_brief)
            with ctx.span("workspace-upload", "api") as upload_span:
                ref = self.deps.workspace.upload_document(
                    brief_mod.brief_filename(campaign_brief), docx_bytes
                )
            self._emit(
                ctx,
                "tool_execution",
                **{
                    "gen_ai.tool.name": "workspace.upload_document",
                    "shiftai.span.id": upload_span.span_id,
                    "shiftai.span.duration_ms": upload_span.duration_ms,
                    "shiftai.action.external_ref": ref,
                },
            )
            return {"doc_ref": ref}

        result, _ = execute_idempotent(key, self.deps.idempotency, side_effect)
        return str(result["doc_ref"])

    def _route_for_approval(
        self,
        ctx: RunContext,
        campaign_brief: CampaignBrief,
        doc_ref: str,
        version: int,
        *,
        control: dict[str, str],
    ) -> None:
        """The routing action: create the approval task, emit action_taken (idempotent)."""
        idempotency_key = f"{ctx.case_id}:route_for_approval:v{version}"

        def side_effect() -> dict[str, Any]:
            task = db.save_approval_task(
                self.deps.store, ctx.case_id, campaign_brief, doc_ref, self._approval_queue()
            )
            return {"task_id": task["task_id"]}

        with ctx.span("l4-action", "api") as action_span:
            result, was_repeat = execute_idempotent(
                idempotency_key, self.deps.idempotency, side_effect
            )
        self._emit(
            ctx,
            "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "route_for_approval",
                "shiftai.action.idempotency_key": idempotency_key,
                "shiftai.action.external_ref": doc_ref,
                "shiftai.action.task_id": str(result["task_id"]),
                "shiftai.control.kill_switch": control["kill_switch"],
                "shiftai.control.rate_breaker": control["rate_breaker"],
                "shiftai.span.id": action_span.span_id,
                "shiftai.span.duration_ms": action_span.duration_ms,
                "shiftai.action.repeat": was_repeat,
            },
        )
        if not was_repeat:
            self.deps.rate_breaker.record_execution(self.deps.config.agent_id)

    # -------------------------------------------------- requester iteration loop

    def revise_brief(
        self,
        case_id: str,
        *,
        directive: str,
        aspects: list[str] | None = None,
        actor_id: str,
        actor_role: str = "marketing-lead",
    ) -> ProcessOutcome:
        """One directive round: the agent rewrites requester-provided fields
        (objective / offer topic) per the human's instruction. Works while the case
        is with the requester (awaiting_input or draft_review); every round is a
        recorded human_gate 'modified'."""
        aspects = aspects or []
        if not directive.strip() and not aspects:
            raise ApprovalGateError("a revision needs a directive note or at least one aspect")
        case = self._load_case_or_raise(case_id, expected={"awaiting_input", "draft_review"})
        approval = record_approval(decision="modified", actor_role=actor_role, actor_id=actor_id)
        request = db.request_from_case(case)
        ctx = RunContext(case_id=case_id, trace_id=str(case["trace_id"]))
        s_hash = scenario_hash(None, str(case.get("action_class")))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "revision_directive",
                "shiftai.learn.agent_recommendation": case.get("action_class"),
                "shiftai.learn.human_action": f"directive: {', '.join(aspects) or directive[:80]}",
                "shiftai.learn.scenario_hash": s_hash,
            },
        )
        db.save_human_decision(self.deps.store, case_id, approval, s_hash)

        with ctx.span("l3-revision", "llm") as revise_span:
            revised, response = revise_request_fields(
                self.deps.provider,
                system_blocks(self.deps.config),
                request,
                directive=directive,
                aspects=aspects,
            )
        if response is not None:
            cost = rate_card_cost(
                MODEL_ID,
                response.input_tokens,
                response.output_tokens,
                response.cache_read_input_tokens,
            )
            ctx.add_cost(cost)
            attrs: dict[str, Any] = {
                "shiftai.layer": "L3",
                "gen_ai.tool.name": "layer3.revise_fields",
                "shiftai.span.id": revise_span.span_id,
                "shiftai.span.duration_ms": revise_span.duration_ms,
                **self._llm_attributes(response),
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
            self._emit(ctx, "tool_execution", **attrs)

        status = str(case.get("status"))
        updated = {
            **case,
            "request": revised.model_dump(),
            "last_directive": {"note": directive, "aspects": aspects, "by": actor_role},
            "run_cost_usd": float(case.get("run_cost_usd", 0.0)) + ctx.total_cost_usd,
        }
        brief_model: CampaignBrief | None = None
        doc_ref = str(case.get("doc_ref", "")) or None
        if status == "draft_review" and case.get("brief"):
            prior = CampaignBrief.model_validate(case["brief"])
            version = int(case.get("brief_version", prior.version)) + 1
            brief_model = brief_mod.assemble_brief(
                case_id=case_id,
                request=revised,
                classification=prior.classification,
                conflicts=prior.conflicts,
                bc_fo=prior.bc_fo or BcFoCheck(mixed=False),
                normalized_fields={},
                version=version,
                campaign_id=prior.campaign_id,
            )
            doc_ref = self._upload_brief(ctx, brief_model)
            updated.update(
                {"brief": brief_model.model_dump(), "brief_version": version, "doc_ref": doc_ref}
            )
        db.save_case(self.deps.store, case_id, updated)
        gap = self.deps.store.get(db.KIND_GAP_REQUEST, case_id)
        return ProcessOutcome(
            case_id=case_id,
            trace_id=ctx.trace_id,
            status=status,  # type: ignore[arg-type]
            action_class=str(case.get("action_class")) if case.get("action_class") else None,
            brief=brief_model,
            gap_request=(
                GapRequest.model_validate(gap.value) if gap and status == "awaiting_input" else None
            ),
            doc_ref=doc_ref,
        )

    def release_brief(
        self,
        case_id: str,
        *,
        actor_id: str,
        actor_role: str = "marketing-lead",
    ) -> ProcessOutcome:
        """The requester's explicit verification: routes the held draft to the BU
        Campaign Lead. Kill switch / rate breaker are checked before the action."""
        case = self._load_case_or_raise(case_id, expected={"draft_review"})
        approval = record_approval(decision="approved", actor_role=actor_role, actor_id=actor_id)
        ctx = RunContext(case_id=case_id, trace_id=str(case["trace_id"]))
        brief_model = CampaignBrief.model_validate(case["brief"])
        s_hash = scenario_hash(brief_model, str(case.get("action_class")))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "approved",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "none",
                "shiftai.learn.agent_recommendation": case.get("action_class"),
                "shiftai.learn.human_action": "verified_and_released",
                "shiftai.learn.scenario_hash": s_hash,
            },
        )
        db.save_human_decision(self.deps.store, case_id, approval, s_hash)

        kill_state, breaker_state, pause_reason = guard_layer4(
            self.deps.kill_switch,
            self.deps.rate_breaker,
            self.deps.config.agent_id,
            self.deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            self._emit(
                ctx,
                "case_escalated",
                **{
                    "shiftai.layer": "escalation",
                    "shiftai.escalation.tier": 2,
                    "shiftai.escalation.reason": "policy_gap",
                    "shiftai.escalation.routed_to": self.deps.config.route_for("policy_gap"),
                    "shiftai.escalation.uncertainty_type": "policy_gap",
                    "shiftai.control.kill_switch": kill_state,
                    "shiftai.control.rate_breaker": breaker_state,
                    "shiftai.context_package": json.dumps({"control_pause_reason": pause_reason}),
                },
            )
            db.save_case(self.deps.store, case_id, {**case, "status": "escalated"})
            return ProcessOutcome(
                case_id=case_id,
                trace_id=ctx.trace_id,
                status="escalated",
                action_class="route_for_approval",
                escalation_reason="control_pause",
            )
        doc_ref = str(case.get("doc_ref", ""))
        self._route_for_approval(
            ctx,
            brief_model,
            doc_ref,
            int(case.get("brief_version", brief_model.version)),
            control={"kill_switch": kill_state, "rate_breaker": breaker_state},
        )
        db.save_case(
            self.deps.store,
            case_id,
            {**case, "status": "awaiting_approval", "awaiting_since": _now()},
        )
        return ProcessOutcome(
            case_id=case_id,
            trace_id=ctx.trace_id,
            status="awaiting_approval",
            action_class="route_for_approval",
            brief=brief_model,
            doc_ref=doc_ref or None,
        )

    def _return_brief(
        self,
        case_id: str,
        *,
        actor_role: str,
        actor_id: str,
        notes: str | None,
    ) -> ProcessOutcome:
        """BU Campaign Lead returns the brief with feedback: back to draft_review
        with the note stored as the pending directive. Not terminal — no resolution."""
        case = self._load_case_or_raise(case_id, expected={"awaiting_approval"})
        approval = record_approval(
            decision="modified", actor_role=actor_role, actor_id=actor_id, notes=notes
        )
        ctx = RunContext(case_id=case_id, trace_id=str(case["trace_id"]))
        brief_model = CampaignBrief.model_validate(case["brief"]) if case.get("brief") else None
        s_hash = scenario_hash(brief_model, str(case.get("action_class")))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "returned_with_note",
                "shiftai.learn.agent_recommendation": case.get("action_class"),
                "shiftai.learn.human_action": "returned_with_note",
                "shiftai.learn.scenario_hash": s_hash,
                "shiftai.learn.decision_latency_ms": _elapsed_ms(
                    str(case.get("awaiting_since", ""))
                ),
            },
        )
        db.save_human_decision(self.deps.store, case_id, approval, s_hash)
        db.save_case(
            self.deps.store,
            case_id,
            {
                **case,
                "status": "draft_review",
                "hold_for_verification": True,
                "returned_note": notes or "",
                "last_directive": {"note": notes or "", "aspects": [], "by": actor_role},
            },
        )
        return ProcessOutcome(
            case_id=case_id,
            trace_id=ctx.trace_id,
            status="draft_review",
            action_class=str(case.get("action_class")) if case.get("action_class") else None,
            brief=brief_model,
            doc_ref=str(case.get("doc_ref")) if case.get("doc_ref") else None,
        )

    # ---------------------------------------------------------------- human gate

    def record_human_decision(
        self,
        case_id: str,
        decision: str,
        *,
        actor_role: str,
        actor_id: str,
        notes: str | None = None,
    ) -> ProcessOutcome:
        """BU Campaign Lead gate: approve / reject / return-with-note. The only path
        that can advance a brief past intake (guardrail 2). ``returned`` sends the
        brief back to the requester (draft_review) with the note as feedback —
        recorded as hitl 'modified', never terminal."""
        if decision == "modified":
            raise ApprovalGateError(
                "use submit_gap_answers or revise_brief for requester modifications; "
                "the approval gate records approved/rejected/returned only"
            )
        if decision == "returned":
            return self._return_brief(
                case_id, actor_role=actor_role, actor_id=actor_id, notes=notes
            )
        case = self._load_case_or_raise(case_id, expected={"awaiting_approval", "escalated"})
        approval = record_approval(
            decision=decision, actor_role=actor_role, actor_id=actor_id, notes=notes
        )
        ctx = RunContext(case_id=case_id, trace_id=str(case["trace_id"]))
        agent_recommendation = case.get("action_class")
        brief_model: CampaignBrief | None = None
        if case.get("brief"):
            brief_model = CampaignBrief.model_validate(case["brief"])
        s_hash = scenario_hash(brief_model, str(agent_recommendation))
        latency_ms = _elapsed_ms(str(case.get("awaiting_since", "")))
        self._emit(
            ctx,
            "human_gate",
            **{
                "shiftai.hitl.decision": approval.decision,
                "shiftai.hitl.actor.role": approval.actor_role,
                "shiftai.learn.reason_code": case.get("escalation_reason_code", "none"),
                "shiftai.learn.agent_recommendation": agent_recommendation,
                "shiftai.learn.human_action": approval.decision,
                "shiftai.learn.label": learning_label(str(agent_recommendation), approval.decision),
                "shiftai.learn.scenario_hash": s_hash,
                "shiftai.learn.occurrence_count_90d": db.occurrence_count_90d(
                    self.deps.store, s_hash
                ),
                "shiftai.learn.calibration_id": new_id("cal"),
                "shiftai.learn.decision_latency_ms": latency_ms,
            },
        )
        db.save_human_decision(self.deps.store, case_id, approval, s_hash)

        released: CampaignBrief | None
        if approval.decision == "approved" and brief_model is not None:
            released = brief_model.model_copy(update={"status": "approved"})
            db.save_approved_brief(self.deps.store, released, str(case.get("doc_ref", "")))
            db.register_campaign(self.deps.store, released)
            new_status, outcome = "approved", "success"
        elif approval.decision == "approved":
            raise ApprovalGateError("cannot approve a case that has no routed brief")
        else:
            released = (
                brief_model.model_copy(update={"status": "rejected"}) if brief_model else None
            )
            new_status, outcome = "rejected", "cancelled"

        db.save_case(
            self.deps.store,
            case_id,
            {**case, "status": new_status, "approval": approval.model_dump()},
        )
        self._emit(
            ctx,
            "case_resolved",
            **{
                "shiftai.layer": "resolution",
                "shiftai.outcome": outcome,
                "shiftai.resolution.outcome_source": "human",
            },
        )
        prior_cost = float(case.get("run_cost_usd", 0.0))
        summary: dict[str, Any] = {
            "shiftai.outcome": outcome,
            "shiftai.hitl.decision": approval.decision,
            **self._latency_attrs(ctx),
        }
        if prior_cost > 0:
            summary.update(
                {
                    "shiftai.cost.amount": prior_cost,
                    "shiftai.cost.currency": "USD",
                    "shiftai.cost.model": "rate_card",
                    "shiftai.cost.scope": "run_total",
                }
            )
        self._emit(ctx, "run_summary", **summary)
        return ProcessOutcome(
            case_id=case_id,
            trace_id=ctx.trace_id,
            status=new_status,  # type: ignore[arg-type]
            action_class=str(agent_recommendation) if agent_recommendation else None,
            brief=released if approval.decision == "approved" else brief_model,
            doc_ref=str(case.get("doc_ref")) if case.get("doc_ref") else None,
        )

    # ------------------------------------------------------------------ helpers

    def _handle_gaps(
        self,
        ctx: RunContext,
        request: CampaignRequest,
        validation: ValidationResult,
        gap_round: int,
        *,
        hold: bool = False,
    ) -> ProcessOutcome:
        config = self.deps.config
        next_round = gap_round + 1
        self._emit(
            ctx,
            "decision_made",
            **{
                "shiftai.layer": "L2",
                "shiftai.decision.action_class": "request_gaps",
                "shiftai.decision.confidence": 1.0,
                "shiftai.decision.layer": 2,
            },
        )
        if next_round > MAX_GAP_ROUNDS:
            # Requester unresponsive after two gap requests → Marketing Lead.
            return self._escalate(
                ctx,
                request,
                action_class="request_gaps",
                decided_by_layer=2,
                tier=2,
                reason="policy_gap",
                uncertainty="data_ambiguity",
                routed_to=config.route_for("policy_gap"),
                reason_code="requester_unresponsive",
                detail={"gap_rounds": gap_round},
                validation=validation,
                conflicts=[],
                already_decided=True,
            )
        with ctx.span("gap-drafting", "llm"):
            gap_request, response = draft_gap_request(
                self.deps.provider,
                system_blocks(config),
                request,
                validation.missing,
                round_number=next_round,
                case_id=ctx.case_id,
            )
        llm_attrs = self._llm_attributes(response) if response else {}
        db.save_gap_request(self.deps.store, gap_request)
        self._emit(
            ctx,
            "case_escalated",
            **{
                "shiftai.layer": "escalation",
                "shiftai.escalation.tier": 1,
                "shiftai.escalation.reason": "policy_gap",
                "shiftai.escalation.routed_to": self.deps.config.route_for("data_ambiguity"),
                "shiftai.escalation.uncertainty_type": "data_ambiguity",
                "shiftai.learn.reason_code": ",".join(gap_reason_codes(validation.missing, config)),
                **llm_attrs,
            },
        )
        db.save_case(
            self.deps.store,
            ctx.case_id,
            {
                "status": "awaiting_input",
                "request": request.model_dump(),
                "trace_id": ctx.trace_id,
                "gap_rounds": next_round,
                "action_class": "request_gaps",
                "escalation_reason_code": "missing_field",
                "hold_for_verification": hold,
                "awaiting_since": _now(),
                "run_cost_usd": ctx.total_cost_usd,
            },
        )
        self._save_context(ctx, request, "awaiting_input", None, validation, [], next_round)
        return ProcessOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            status="awaiting_input",
            action_class="request_gaps",
            gap_request=gap_request,
            escalation_reason="data_ambiguity",
        )

    def _escalate(
        self,
        ctx: RunContext,
        request: CampaignRequest,
        *,
        action_class: str | None,
        decided_by_layer: int,
        tier: int,
        reason: str,
        uncertainty: str,
        routed_to: str,
        reason_code: str,
        detail: dict[str, Any],
        validation: ValidationResult,
        conflicts: list[ConflictFlag],
        control: dict[str, str] | None = None,
        already_decided: bool = False,
    ) -> ProcessOutcome:
        if not already_decided:
            self._emit(
                ctx,
                "decision_made",
                **{
                    "shiftai.layer": f"L{decided_by_layer}",
                    "shiftai.decision.action_class": action_class,
                    "shiftai.decision.confidence": 1.0 if decided_by_layer == 2 else 0.0,
                    "shiftai.decision.layer": decided_by_layer,
                },
            )
        attrs: dict[str, Any] = {
            "shiftai.layer": "escalation",
            "shiftai.escalation.tier": tier,
            "shiftai.escalation.reason": reason,
            "shiftai.escalation.routed_to": routed_to,
            "shiftai.escalation.uncertainty_type": uncertainty,
            "shiftai.learn.reason_code": reason_code,
            "shiftai.context_package": json.dumps(detail, default=str),
        }
        if control:
            attrs["shiftai.control.kill_switch"] = control["kill_switch"]
            attrs["shiftai.control.rate_breaker"] = control["rate_breaker"]
        self._emit(ctx, "case_escalated", **attrs)
        db.save_case(
            self.deps.store,
            ctx.case_id,
            {
                "status": "escalated",
                "request": request.model_dump(),
                "trace_id": ctx.trace_id,
                "action_class": action_class,
                "escalation_reason_code": reason_code,
                "escalation_detail": detail,
                "awaiting_since": _now(),
                "run_cost_usd": ctx.total_cost_usd,
            },
        )
        self._save_context(ctx, request, "escalated", None, validation, conflicts, 0)
        return ProcessOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            status="escalated",
            action_class=action_class,
            escalation_reason=reason_code,
        )

    def _fail(
        self,
        ctx: RunContext,
        raw: dict[str, Any],
        request: CampaignRequest,
        exc: Exception,
    ) -> ProcessOutcome:
        error_type = type(exc).__name__
        try:
            db.save_failed_request(self.deps.store, ctx.case_id, raw, error_type, str(exc))
            db.save_case(
                self.deps.store,
                ctx.case_id,
                {
                    "status": "failed",
                    "request": request.model_dump(),
                    "trace_id": ctx.trace_id,
                    "error_type": error_type,
                },
            )
        except Exception:
            # The store itself is down: telemetry (below) is the surviving record;
            # the raw request is still in the caller's hands, never silently dropped.
            pass
        self._emit(ctx, "error", **{"error.type": error_type, "shiftai.outcome": "failure"})
        self._emit(
            ctx,
            "run_summary",
            **{
                "shiftai.outcome": "failure",
                "error.type": error_type,
                **self._latency_attrs(ctx),
            },
        )
        return ProcessOutcome(
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            status="failed",
            action_class=None,
            escalation_reason=error_type,
        )

    def _emit_l3_decision(
        self, ctx: RunContext, output: ClassifyOutput, response: LLMResponse
    ) -> None:
        cost = rate_card_cost(
            MODEL_ID,
            response.input_tokens,
            response.output_tokens,
            response.cache_read_input_tokens,
        )
        ctx.add_cost(cost)
        span = ctx.spans[-1]
        attrs: dict[str, Any] = {
            "shiftai.layer": "L3",
            "shiftai.decision.action_class": output.action_class,
            "shiftai.decision.confidence": output.confidence,
            "shiftai.decision.layer": 3,
            "shiftai.span.id": span.span_id,
            "shiftai.span.duration_ms": span.duration_ms,
            **self._llm_attributes(response),
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

    def _llm_attributes(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "gen_ai.request.model": MODEL_ID,
            "gen_ai.response.model": response.model,
            "gen_ai.usage.input_tokens": response.input_tokens,
            "gen_ai.usage.output_tokens": response.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": response.cache_read_input_tokens,
            "shiftai.model.version": response.model,
            "shiftai.prompt.template.id": PROMPT_TEMPLATE_ID,
            "shiftai.prompt.template.version": PROMPT_TEMPLATE_VERSION,
            "shiftai.prompt.system.version": SYSTEM_PROMPT_VERSION,
        }

    def _save_context(
        self,
        ctx: RunContext,
        request: CampaignRequest,
        status: str,
        output: ClassifyOutput | None,
        validation: ValidationResult,
        conflicts: list[ConflictFlag],
        gap_rounds: int,
    ) -> None:
        case = db.load_case(self.deps.store, ctx.case_id) or {}
        db.save_intake_context(
            self.deps.store,
            IntakeContext(
                case_id=ctx.case_id,
                campaign_id=case.get("campaign_id"),
                request_source=request.source,
                status=status,  # type: ignore[arg-type]
                classification=output.classification if output else None,
                conflicts=conflicts,
                validation=validation,
                approval=None,
                gap_rounds=gap_rounds,
            ),
        )

    def _approval_queue(self) -> str:
        return self.deps.config.route_for("confidence_only")

    def _load_case_or_raise(self, case_id: str, expected: set[str]) -> dict[str, Any]:
        case = db.load_case(self.deps.store, case_id)
        if case is None:
            raise ApprovalGateError(f"unknown case {case_id!r}")
        if case.get("status") not in expected:
            raise ApprovalGateError(
                f"case {case_id!r} is {case.get('status')!r}; expected one of {sorted(expected)}"
            )
        return case

    def _check_budget(self, ctx: RunContext) -> None:
        if ctx.latency_breakdown_ms()["total"] > RUN_TIMEOUT_S * 1000:
            raise RunTimeoutError(f"processing exceeded {RUN_TIMEOUT_S}s budget")

    def _latency_attrs(self, ctx: RunContext) -> dict[str, Any]:
        lat = ctx.latency_breakdown_ms()
        return {
            "shiftai.run.id": ctx.run_id,
            "shiftai.latency.llm_ms": lat["llm"],
            "shiftai.latency.api_ms": lat["api"],
            "shiftai.latency.queue_ms": lat["queue"],
        }

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
