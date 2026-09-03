"""The Content Repurposing case state machine.

Flagship pass (spec steps 1-4): approved outline + pack loaded from the Context
Store → unverifiable sections refused up front (gap notes) → L3 long-form draft
with inline [c-N] markers → grounding strips anything citing outside the verified
proof points → deterministic self-check (regenerate ≤ config limit, then withhold)
→ versioned .docx + claim-map staged in the campaign workspace.

Human gate: ``confirm_flagship`` records the identity-stamped content-confirmed
decision (production: the Content Collaboration Agent carries it; dev: the studio
stand-in). No code path in this agent calls it.

Fan-out (spec steps 5-9): runs ONLY from a human-confirmed flagship — the state
machine refuses anything else and emits a ``sequencing_violation`` escalation
(spec Alerting). The confirmed flagship's claim inventory (quotes verified
verbatim in code; deterministic fallback from the claim map, never invented) is
the single source for every derivative; lineage is recorded per derivative; a
failed self-check withholds the asset (stage the passing subset — spec Fallback).

Rework (spec step 10): regenerates only the affected asset as a NEW version;
reviewed content is never overwritten.

STS mapping: deterministic policy escalations carry
``shiftai.escalation.reason=policy_gap`` with the precise code in the additive
``shiftai.learn.reason_code``; human "confirmed" maps to hitl ``approved``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import AssetChecklist
from c2c_campaign_box.workspace import CampaignWorkspace, WorkspaceWriteError
from shiftai_shared.brand import BrandRules
from shiftai_shared.config import SharedSettings, runtime_rate_card
from shiftai_shared.context_store.store import ContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.llm import LLMProvider, LLMResponse, SystemBlock
from shiftai_shared.resilience import IdempotencyStore, execute_idempotent
from shiftai_shared.telemetry import StsEmitter, TelemetrySink
from shiftai_shared.telemetry.envelope import RunContext, new_id, response_cost

from c2c_content_repurposing import (
    AGENT_TYPE,
    DATA_CLASSIFICATION,
    DERIVATIVE_MAX_TOKENS,
    DERIVATIVE_TIMEOUT_S,
    FANOUT_TIMEOUT_S,
    FLAGSHIP_MAX_TOKENS,
    FLAGSHIP_TIMEOUT_S,
    MODEL_ID,
    PROCESS_NAME,
    RISK_TIER,
    SYSTEM_PROMPT_VERSION,
)
from c2c_content_repurposing import documents as docs
from c2c_content_repurposing import generation as gen
from c2c_content_repurposing import persistence as db
from c2c_content_repurposing.agent_config import RepurposingConfig
from c2c_content_repurposing.fanout import DerivativeJob, build_fanout_jobs, run_fanout_jobs
from c2c_content_repurposing.grounding import (
    deterministic_inventory,
    ground_derivative,
    ground_flagship,
    numeric_tokens,
    verify_inventory_items,
)
from c2c_content_repurposing.intake import (
    DraftingContext,
    FlagshipOutlineMissingError,
    PlanNotReadyError,
    load_drafting_context,
)
from c2c_content_repurposing.models import (
    ClaimInventory,
    ClaimMarker,
    DerivativeLLMOutput,
    DraftSection,
    FanoutOutcome,
    FlagshipLLMOutput,
    FlagshipOutcome,
    GapNote,
    InventoryLLMOutput,
    RepurposeStatus,
    ReworkOutcome,
    SelfCheckReport,
    StagedDraft,
)
from c2c_content_repurposing.selfcheck import failure_feedback, run_self_check


class RepurposeGateError(Exception):
    """A gate violation: wrong state, unknown case, or a forbidden transition."""


class SequencingViolationError(RepurposeGateError):
    """Fan-out attempted before the flagship is human-confirmed (spec guardrail 3)."""


class RunTimeoutError(Exception):
    """The per-run processing budget was exceeded (spec Timeout)."""


@dataclass
class RepurposingDeps:
    provider: LLMProvider
    store: ContextStore
    workspace: CampaignWorkspace  # the SAME campaign workspace Agent 2 created
    sink: TelemetrySink
    kill_switch: KillSwitch
    rate_breaker: RateBreaker
    idempotency: IdempotencyStore
    config: RepurposingConfig
    settings: SharedSettings
    brand_rules: BrandRules


class ContentRepurposingAgent:
    def __init__(self, deps: RepurposingDeps) -> None:
        self.deps = deps
        # Fleet rate card: cost is priced by the model that actually answered.
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

    # ------------------------------------------------------------ flagship pass

    def draft_flagship(
        self, campaign_id: str, *, trace_id: str | None = None
    ) -> FlagshipOutcome:
        """Steps 1-4. Triggered on outline approval (plan confirmed). The campaign
        trace_id keeps the journey on one trace across agents 1-3."""
        case = db.load_case(self.deps.store, campaign_id)
        if case is not None and case.get("status") not in {"failed", "escalated"}:
            raise RepurposeGateError(
                f"campaign {campaign_id!r} already has a flagship in "
                f"{case.get('status')!r}; use rework to regenerate"
            )
        ctx = RunContext(case_id=campaign_id, trace_id=trace_id or new_id("trace"))
        try:
            context = load_drafting_context(self.deps.store, self.deps.config, campaign_id)
        except (PlanNotReadyError, FlagshipOutlineMissingError) as exc:
            db.save_failed_run(self.deps.store, campaign_id, type(exc).__name__, str(exc))
            self._emit(ctx, "error", **{"error.type": type(exc).__name__,
                                        "shiftai.outcome": "failure"})
            self._emit(ctx, "run_summary", **{"shiftai.outcome": "failure",
                                              "error.type": type(exc).__name__,
                                              **self._latency_attrs(ctx)})
            return FlagshipOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="failed", escalation_reasons=["plan_not_ready"],
            )
        if not trace_id and context.box_trace_id:
            ctx = RunContext(case_id=campaign_id, trace_id=context.box_trace_id)
        try:
            return self._flagship_pipeline(ctx, campaign_id, context)
        except Exception as exc:  # fail-closed: persist, emit, never discard
            return self._fail(ctx, campaign_id, exc, FlagshipOutcome)

    def _flagship_pipeline(
        self, ctx: RunContext, campaign_id: str, context: DraftingContext
    ) -> FlagshipOutcome:
        deps = self.deps
        config = deps.config
        flagship_id = config.flagship_asset_type
        self._emit(
            ctx, "case_intake",
            **{
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{flagship_id}",
            },
        )
        self._emit(ctx, "config_loaded")
        self._emit(
            ctx, "policy_check",
            **{
                "shiftai.layer": "L2",
                "shiftai.policy.ids": ["approved_outline_only", "sourced_claims_only",
                                       "no_publish_surface"],
                "shiftai.policy.decision": "allow",
                "shiftai.outline.sections_total": len(context.flagship_outline.sections),
                "shiftai.outline.sections_draftable": len(context.draftable_sections),
                "shiftai.outline.sections_refused": len(context.unverified_gap_notes),
            },
        )
        gap_notes = list(context.unverified_gap_notes)
        if not context.draftable_sections:
            db.save_gap_notes(deps.store, campaign_id, gap_notes)
            self._escalation_event(
                ctx, tier=2, reason_code="unsourced_claim",
                detail={"note": "every outline section demands claims the pack cannot source",
                        "gap_notes": [g.model_dump() for g in gap_notes]},
            )
            self._save_case(campaign_id, ctx, status="escalated", extra={
                "flagship_asset_id": flagship_id, "flagship_version": 0,
                "folder": context.folder, "campaign_slug": context.campaign_slug,
            })
            self._emit(ctx, "run_summary", **{"shiftai.outcome": "partial",
                                              **self._latency_attrs(ctx)})
            return FlagshipOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="escalated", gap_notes=gap_notes,
                escalation_reasons=["unsourced_claim"],
            )

        payload = {
            "approved_outline": {
                "title": context.flagship_outline.title,
                "sections": context.draftable_sections,
            },
            "audience_offer_pack": context.pack.model_dump(
                include={"vertical", "personas", "value_proposition", "differentiators",
                         "messaging_angles", "ctas", "channel_emphasis"}
            ),
            "verified_proof_points": [p.model_dump() for p in context.pack.proof_points],
        }
        blocks = gen.system_blocks(config, deps.brand_rules)

        draft_result = self._generate_flagship(ctx, campaign_id, context, blocks, payload)
        if draft_result is None:
            gap = GapNote(
                gap_id=f"gap_{campaign_id}_{flagship_id}_unparsable",
                asset_id=flagship_id, section="(whole draft)",
                needed="flagship model output unparsable after retry — human drafting needed",
            )
            gap_notes.append(gap)
            db.save_gap_notes(deps.store, campaign_id, gap_notes)
            self._escalation_event(ctx, tier=2, reason_code="tool_failure",
                                   detail={"note": "flagship output unparsable after retry"})
            self._save_case(campaign_id, ctx, status="escalated", extra={
                "flagship_asset_id": flagship_id, "flagship_version": 0,
                "folder": context.folder, "campaign_slug": context.campaign_slug,
            })
            # "partial" — the case is escalated with explicit gaps, not a crash
            # (schema: outcome=failure requires error.type).
            self._emit(ctx, "run_summary", **{"shiftai.outcome": "partial",
                                              **self._latency_attrs(ctx)})
            return FlagshipOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="escalated", gap_notes=gap_notes, escalation_reasons=["tool_failure"],
            )

        title, sections, markers, draft_gaps, report = draft_result
        gap_notes.extend(draft_gaps)
        escalations: list[str] = []

        if not sections:
            escalations.append("unsourced_claim")
            self._escalation_event(
                ctx, tier=2, reason_code="unsourced_claim",
                detail={"note": "every drafted section was stripped by grounding",
                        "gap_notes": [g.model_dump() for g in draft_gaps]},
            )
        if not report.passed:
            escalations.append("selfcheck_failed")
            self._escalation_event(
                ctx, tier=2, reason_code="selfcheck_failed",
                detail={"attempts": report.attempts,
                        "findings": report.findings,
                        "unsourced_numeric_tokens": report.unsourced_numeric_tokens},
            )

        staged = bool(sections) and report.passed
        version = self._next_version(campaign_id, flagship_id)
        draft = StagedDraft(
            campaign_id=campaign_id,
            asset_id=flagship_id,
            asset_type=flagship_id,
            kind="flagship",
            title=title or context.flagship_outline.title,
            version=version,
            filename=docs.draft_filename(context.campaign_slug, flagship_id, version),
            file_ref="",
            claim_map_ref="",
            sections=sections,
            claim_markers=markers,
            claim_lineage=[],
            self_check=report,
            gap_notes=gap_notes,
            status="staged" if staged else "withheld",
            created_at=_now(),
        )
        if staged:
            draft = self._stage_draft(ctx, draft, context.folder)
            staged = draft.status == "staged"  # kill-switch pause can still withhold
        db.save_draft(deps.store, draft)
        db.save_gap_notes(deps.store, campaign_id, gap_notes)

        status: RepurposeStatus = "flagship_staged" if staged else "escalated"
        self._save_case(campaign_id, ctx, status=status, extra={
            "flagship_asset_id": flagship_id,
            "flagship_version": version,
            "folder": context.folder,
            "campaign_slug": context.campaign_slug,
            "escalations": escalations,
            "awaiting_since": _now(),
        })
        self._emit(
            ctx, "run_summary",
            **{
                "shiftai.outcome": "partial" if (escalations or gap_notes) else "success",
                **self._latency_attrs(ctx),
                **(self._cost_attrs(ctx.total_cost_usd) if ctx.total_cost_usd > 0 else {}),
            },
        )
        return FlagshipOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status=status, draft=draft, gap_notes=gap_notes, escalation_reasons=escalations,
        )

    def _generate_flagship(
        self,
        ctx: RunContext,
        campaign_id: str,
        context: DraftingContext,
        blocks: list[SystemBlock],
        payload: dict[str, Any],
        instruction: str | None = None,
    ) -> tuple[str, list[DraftSection], list[ClaimMarker], list[GapNote], SelfCheckReport] | None:
        """Generate → ground → self-check loop (regenerate ≤ max, then withhold).
        Returns None only when the model output stayed unparsable."""
        config = self.deps.config
        feedback: list[str] | None = None
        last: tuple[str, list[DraftSection], list[ClaimMarker], list[GapNote],
                    SelfCheckReport] | None = None
        for attempt in range(1, config.max_regenerations + 2):
            with ctx.span("l3-flagship-draft", "llm") as span:
                output, response, truncated = gen.run_json_call(
                    self.deps.provider, blocks,
                    gen.flagship_user_prompt(payload, feedback, instruction),
                    FlagshipLLMOutput,
                    max_tokens=FLAGSHIP_MAX_TOKENS,
                    timeout_s=FLAGSHIP_TIMEOUT_S,
                    truncation_raise_factor=config.truncation_raise_factor,
                )
            self._emit_l3(
                ctx, span.span_id, span.duration_ms, response,
                template_id=gen.FLAGSHIP_TEMPLATE_ID,
                action="flagship_draft",
                confidence=output.confidence if output else 0.0,
                extra={
                    "shiftai.generation.attempt": attempt,
                    "shiftai.generation.truncation_retried": truncated,
                    "shiftai.business_object.type": "campaign_asset",
                    "shiftai.business_object.id": f"{campaign_id}:{config.flagship_asset_type}",
                },
            )
            self._check_budget(ctx, FLAGSHIP_TIMEOUT_S)
            if output is None:
                return last
            sections, markers, gaps = ground_flagship(
                output, context.verified_refs, campaign_id, config.flagship_asset_type
            )
            text = " ".join([output.title, *(p for s in sections for p in s.paragraphs)])
            claim_text = " ".join(m.claim for m in markers).lower()
            unsourced = [t for t in numeric_tokens(text) if _digits(t) not in claim_text]
            report = run_self_check(
                text, self.deps.brand_rules,
                unsourced_numeric_tokens=unsourced, attempts=attempt,
            )
            last = (output.title, sections, markers, gaps, report)
            if report.passed or attempt > config.max_regenerations:
                return last
            feedback = failure_feedback(report)
        return last

    # ------------------------------------------------------------- human gate

    def confirm_flagship(
        self,
        campaign_id: str,
        *,
        actor_id: str,
        actor_role: str = "content-writer",
        notes: str | None = None,
    ) -> FlagshipOutcome:
        """The content-confirmed human gate (production: carried by the Content
        Collaboration Agent; dev: the studio stand-in). This agent never calls it —
        fan-out is impossible without the record it writes."""
        case = self._load_case_or_raise(campaign_id, expected={"flagship_staged"})
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        draft = db.latest_draft(self.deps.store, campaign_id,
                                str(case["flagship_asset_id"]))
        if draft is None or draft.status != "staged":
            raise RepurposeGateError(
                f"campaign {campaign_id!r} has no staged flagship draft to confirm"
            )
        confirmation = {
            "kind": "flagship_content",
            "decision": "confirmed",
            "actor_id": actor_id,
            "actor_role": actor_role,
            "timestamp": _now(),
            "flagship_version": draft.version,
            "notes": notes,
        }
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": "approved",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "none",
                "shiftai.learn.agent_recommendation": "flagship_draft",
                "shiftai.learn.human_action": f"content_confirmed:{draft.asset_id}",
                "shiftai.learn.decision_latency_ms": _elapsed_ms(
                    str(case.get("awaiting_since", ""))
                ),
                "shiftai.business_object.type": "campaign_asset",
                "shiftai.business_object.id": f"{campaign_id}:{draft.asset_id}",
            },
        )
        db.save_case(self.deps.store, campaign_id, {
            **case, "status": "flagship_confirmed", "flagship_confirmation": confirmation,
        })
        return FlagshipOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status="flagship_confirmed", draft=draft,
        )

    # ---------------------------------------------------------------- fan-out

    def run_fanout(self, campaign_id: str) -> FanoutOutcome:
        """Steps 5-9. Runs ONLY from a human-confirmed flagship; anything else is a
        sequencing violation — refused AND escalated (spec Alerting)."""
        case = db.load_case(self.deps.store, campaign_id)
        if case is None:
            raise RepurposeGateError(f"unknown repurpose case {campaign_id!r}")
        status = str(case.get("status"))
        if status not in {"flagship_confirmed", "derivatives_staged"} or not case.get(
            "flagship_confirmation"
        ):
            ctx = RunContext(
                case_id=campaign_id,
                trace_id=str(case.get("trace_id", "")) or new_id("trace"),
            )
            self._escalation_event(
                ctx, tier=3, reason_code="sequencing_violation",
                detail={"status": status,
                        "note": "fan-out attempted before flagship confirmation"},
            )
            raise SequencingViolationError(
                f"campaign {campaign_id!r} flagship is {status!r} — derivatives are "
                "generated only from a human-confirmed flagship"
            )
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        try:
            return self._fanout_pipeline(ctx, campaign_id, case)
        except Exception as exc:
            return self._fail(ctx, campaign_id, exc, FanoutOutcome)

    def _fanout_pipeline(
        self, ctx: RunContext, campaign_id: str, case: dict[str, Any]
    ) -> FanoutOutcome:
        deps = self.deps
        config = deps.config
        folder = str(case["folder"])
        slug = str(case["campaign_slug"])
        flagship_version = int(case["flagship_version"])

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
            return FanoutOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="flagship_confirmed", escalation_reasons=["control_pause"],
            )

        flagship = db.latest_draft(deps.store, campaign_id, str(case["flagship_asset_id"]))
        if flagship is None:
            raise RepurposeGateError(f"no flagship draft found for {campaign_id!r}")
        checklist = self._load_checklist(campaign_id)
        blocks = gen.system_blocks(config, deps.brand_rules)

        inventory = self._ensure_inventory(ctx, campaign_id, flagship, flagship_version,
                                           blocks, folder, slug)

        jobs, skipped = build_fanout_jobs(checklist, config)
        already_staged = {
            d.asset_id for d in db.load_drafts(deps.store, campaign_id)
            if d.kind == "derivative" and d.status == "staged"
        }
        jobs = [j for j in jobs if j.asset_id not in already_staged]
        audience_note = {
            "value_proposition": inventory_pack_note(deps.store, campaign_id),
            "campaign_id": campaign_id,
        }

        staged: list[StagedDraft] = []
        withheld: list[str] = []
        gap_notes: list[GapNote] = []

        def worker(job: DerivativeJob) -> None:
            draft = self._generate_derivative(
                ctx, campaign_id, job, inventory, blocks, audience_note,
                folder=folder, slug=slug,
            )
            gap_notes.extend(draft.gap_notes)
            if draft.status == "staged":
                staged.append(draft)
            else:
                withheld.append(job.asset_id)
            self._check_budget(ctx, FANOUT_TIMEOUT_S)

        run_fanout_jobs(jobs, worker)

        escalations: list[str] = []
        if withheld:
            escalations.append("selfcheck_failed")
            self._escalation_event(
                ctx, tier=2, reason_code="selfcheck_failed",
                detail={"withheld_assets": withheld,
                        "note": "failed self-check after regeneration — never staged; "
                                "the passing subset is staged with gap notes for the rest"},
            )
        db.save_gap_notes(deps.store, campaign_id, gap_notes)
        db.save_case(deps.store, campaign_id, {
            **case,
            "status": "derivatives_staged",
            "inventory_version": flagship_version,
            "staged_assets": sorted({*case.get("staged_assets", []),
                                     *(d.asset_id for d in staged)}),
            "withheld_assets": withheld,
            "skipped_assets": skipped,
            "escalations": escalations,
            "run_cost_usd": ctx.total_cost_usd,
        })
        lineage_covered = all(d.claim_lineage for d in staged)
        self._emit(
            ctx, "run_summary",
            **{
                "shiftai.outcome": "partial" if withheld else "success",
                "shiftai.fanout.staged": len(staged),
                "shiftai.fanout.withheld": len(withheld),
                "shiftai.fanout.skipped": len(skipped),
                "shiftai.fanout.claim_lineage_coverage": 1.0 if lineage_covered else 0.0,
                **self._latency_attrs(ctx),
                **(self._cost_attrs(ctx.total_cost_usd) if ctx.total_cost_usd > 0 else {}),
            },
        )
        return FanoutOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status="derivatives_staged", staged=staged, withheld=withheld,
            skipped=skipped, inventory=inventory, gap_notes=gap_notes,
            escalation_reasons=escalations,
        )

    def _ensure_inventory(
        self,
        ctx: RunContext,
        campaign_id: str,
        flagship: StagedDraft,
        flagship_version: int,
        blocks: list[SystemBlock],
        folder: str,
        slug: str,
    ) -> ClaimInventory:
        """Step 5, idempotent per flagship version. LLM extraction verified in code
        (verbatim quotes only); degrade → deterministic inventory from the claim
        map — sourced by construction, never invented."""
        existing = db.load_inventory(self.deps.store, campaign_id, flagship_version)
        if existing is not None:
            return existing
        flagship_text = docs.flagship_plain_text(flagship)
        marker_map = [m.model_dump() for m in flagship.claim_markers]
        with ctx.span("l3-claim-inventory", "llm") as span:
            output, response, truncated = gen.run_json_call(
                self.deps.provider, blocks,
                gen.inventory_user_prompt(flagship_text, marker_map),
                InventoryLLMOutput,
                max_tokens=DERIVATIVE_MAX_TOKENS,
                timeout_s=DERIVATIVE_TIMEOUT_S,
                truncation_raise_factor=self.deps.config.truncation_raise_factor,
            )
        marker_refs = {m.source_ref for m in flagship.claim_markers}
        if output is not None:
            items, dropped = verify_inventory_items(output.items, flagship_text, marker_refs)
        else:
            items, dropped = [], 0
        if items:
            inventory = ClaimInventory(
                campaign_id=campaign_id, flagship_version=flagship_version,
                items=items, method="llm_verified", dropped_unverified=dropped,
                created_at=_now(),
            )
        else:
            inventory = deterministic_inventory(
                flagship.claim_markers, flagship_version, campaign_id, _now()
            )
        self._emit_l3(
            ctx, span.span_id, span.duration_ms, response,
            template_id=gen.INVENTORY_TEMPLATE_ID,
            action="claim_inventory",
            confidence=output.confidence if output else 0.0,
            extra={
                "shiftai.inventory.items": len(inventory.items),
                "shiftai.inventory.dropped_unverified": dropped,
                "shiftai.inventory.method": inventory.method,
                "shiftai.generation.truncation_retried": truncated,
            },
        )
        db.save_inventory(self.deps.store, inventory)
        self._upload_once(
            ctx, f"{campaign_id}:inventory:v{flagship_version}", f"{folder}/drafts",
            docs.inventory_filename(slug, flagship_version), docs.inventory_json(inventory),
        )
        return inventory

    def _generate_derivative(
        self,
        ctx: RunContext,
        campaign_id: str,
        job: DerivativeJob,
        inventory: ClaimInventory,
        blocks: list[SystemBlock],
        audience_note: dict[str, Any],
        *,
        folder: str,
        slug: str,
        instruction: str | None = None,
    ) -> StagedDraft:
        """One derivative: generate → ground → self-check loop → stage or withhold."""
        config = self.deps.config
        feedback: list[str] | None = None
        title = job.recipe.label
        variants_sections: list[DraftSection] = []
        lineage: list[str] = []
        gap_notes: list[GapNote] = []
        report = SelfCheckReport(passed=False, attempts=0)
        for attempt in range(1, config.max_regenerations + 2):
            with ctx.span(f"l3-derivative-{job.asset_type}", "llm") as span:
                output, response, truncated = gen.run_json_call(
                    self.deps.provider, blocks,
                    gen.derivative_user_prompt(
                        job.recipe, job.volume, inventory, audience_note,
                        instruction=instruction, selfcheck_feedback=feedback,
                    ),
                    DerivativeLLMOutput,
                    max_tokens=DERIVATIVE_MAX_TOKENS,
                    timeout_s=DERIVATIVE_TIMEOUT_S,
                    truncation_raise_factor=config.truncation_raise_factor,
                )
            self._emit_l3(
                ctx, span.span_id, span.duration_ms, response,
                template_id=gen.DERIVATIVE_TEMPLATE_ID,
                action=f"derivative:{job.asset_type}",
                confidence=output.confidence if output else 0.0,
                extra={
                    "shiftai.generation.attempt": attempt,
                    "shiftai.generation.truncation_retried": truncated,
                    "shiftai.business_object.type": "campaign_asset",
                    "shiftai.business_object.id": f"{campaign_id}:{job.asset_id}",
                },
            )
            if output is None:
                report = SelfCheckReport(
                    passed=False, attempts=attempt,
                    findings=[{"rule_id": "unparsable_output", "severity": "error",
                               "term": job.asset_type,
                               "detail": "model output unparsable after retry"}],
                )
                break
            variants, lineage, unsourced, gap_notes = ground_derivative(
                output, inventory, volume_cap=job.volume,
                campaign_id=campaign_id, asset_id=job.asset_id,
            )
            title = output.title or job.recipe.label
            variants_sections = [
                DraftSection(
                    heading=v.label or f"{job.recipe.label} — variant {i}",
                    paragraphs=list(v.paragraphs),
                )
                for i, v in enumerate(variants, start=1)
            ]
            text = " ".join(p for s in variants_sections for p in s.paragraphs)
            report = run_self_check(
                text, self.deps.brand_rules,
                unsourced_numeric_tokens=unsourced,
                must_name_brand=job.recipe.must_name_brand,
                attempts=attempt,
            )
            if not variants_sections:
                report = report.model_copy(update={"passed": False})
            if report.passed or attempt > config.max_regenerations:
                break
            feedback = failure_feedback(report)

        staged = report.passed and bool(variants_sections)
        version = self._next_version(campaign_id, job.asset_id)
        if not staged:
            gap_notes = [
                *gap_notes,
                GapNote(
                    gap_id=f"gap_{campaign_id}_{job.asset_id}_withheld",
                    asset_id=job.asset_id,
                    section="(whole asset)",
                    needed=(
                        f"self-check failures persist after {report.attempts} attempt(s) — "
                        "asset withheld, human drafting or rework needed"
                    ),
                ),
            ]
        draft = StagedDraft(
            campaign_id=campaign_id,
            asset_id=job.asset_id,
            asset_type=job.asset_type,
            kind="derivative",
            title=title,
            version=version,
            filename=docs.draft_filename(slug, job.asset_type, version),
            file_ref="",
            claim_map_ref="",
            sections=variants_sections,
            claim_markers=[],
            claim_lineage=lineage,
            self_check=report,
            gap_notes=gap_notes,
            inventory_version=inventory.flagship_version,
            status="staged" if staged else "withheld",
            created_at=_now(),
        )
        if staged:
            draft = self._stage_draft(ctx, draft, folder)
        db.save_draft(self.deps.store, draft)
        return draft

    # ---------------------------------------------------------------- rework

    def apply_rework(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        instruction: str,
        actor_id: str,
        actor_role: str = "content-reviewer",
        rule_codes: list[str] | None = None,
    ) -> ReworkOutcome:
        """Step 10: regenerate ONLY the affected asset as a new version, preserving
        everything else. Flagship rework is possible only before confirmation —
        after it, the derivatives depend on the confirmed claim inventory and the
        change must go through the governance re-open path."""
        if not instruction.strip():
            raise RepurposeGateError("a rework request needs a non-empty instruction")
        case = self._load_case_or_raise(
            campaign_id,
            expected={"flagship_staged", "flagship_confirmed", "derivatives_staged"},
        )
        ctx = RunContext(case_id=campaign_id, trace_id=str(case["trace_id"]))
        status = str(case["status"])
        flagship_id = str(case["flagship_asset_id"])
        is_flagship = asset_id == flagship_id
        if is_flagship and status != "flagship_staged":
            raise RepurposeGateError(
                "the flagship is already content-confirmed; regenerating it would "
                "invalidate the claim inventory — route the change through the "
                "governance re-open path instead"
            )
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": "modified",
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": "rework_request",
                "shiftai.learn.agent_recommendation": "staged_draft",
                "shiftai.learn.human_action": f"rework:{asset_id}",
                "shiftai.context_package": json.dumps(
                    {"instruction": instruction[:500], "rule_codes": rule_codes or [],
                     "actor_id": actor_id}
                ),
            },
        )
        folder = str(case["folder"])
        slug = str(case["campaign_slug"])
        if is_flagship:
            context = load_drafting_context(self.deps.store, self.deps.config, campaign_id)
            payload = {
                "approved_outline": {
                    "title": context.flagship_outline.title,
                    "sections": context.draftable_sections,
                },
                "audience_offer_pack": context.pack.model_dump(
                    include={"vertical", "personas", "value_proposition", "differentiators",
                             "messaging_angles", "ctas", "channel_emphasis"}
                ),
                "verified_proof_points": [p.model_dump() for p in context.pack.proof_points],
            }
            blocks = gen.system_blocks(self.deps.config, self.deps.brand_rules)
            result = self._generate_flagship(
                ctx, campaign_id, context, blocks, payload, instruction=instruction
            )
            if result is None:
                self._escalation_event(ctx, tier=2, reason_code="tool_failure",
                                       detail={"note": "rework output unparsable"})
                return ReworkOutcome(
                    case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                    status="flagship_staged", escalation_reasons=["tool_failure"],
                )
            title, sections, markers, gaps, report = result
            prior = db.latest_draft(self.deps.store, campaign_id, asset_id)
            version = self._next_version(campaign_id, asset_id)
            draft = StagedDraft(
                campaign_id=campaign_id, asset_id=asset_id, asset_type=asset_id,
                kind="flagship", title=title or context.flagship_outline.title,
                version=version,
                filename=docs.draft_filename(slug, asset_id, version),
                file_ref="", claim_map_ref="",
                sections=sections, claim_markers=markers, self_check=report,
                gap_notes=gaps, status="staged" if report.passed and sections else "withheld",
                rework_of_version=prior.version if prior else None,
                created_at=_now(),
            )
            if draft.status == "staged":
                draft = self._stage_draft(ctx, draft, folder)
            db.save_draft(self.deps.store, draft)
            db.save_case(self.deps.store, campaign_id,
                         {**case, "flagship_version": draft.version})
            return ReworkOutcome(
                case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
                status="flagship_staged", draft=draft,
                escalation_reasons=[] if draft.status == "staged" else ["selfcheck_failed"],
            )

        # Derivative rework: regenerate from the SAME confirmed inventory.
        inventory = db.load_inventory(
            self.deps.store, campaign_id, int(case.get("inventory_version", 0))
        )
        if inventory is None:
            raise RepurposeGateError(
                f"no claim inventory exists for {campaign_id!r} — run the fan-out first"
            )
        checklist = self._load_checklist(campaign_id)
        item = next((i for i in checklist.items if i.asset_id == asset_id), None)
        if item is None:
            raise RepurposeGateError(f"asset {asset_id!r} is not on the checklist")
        recipe = self.deps.config.recipe_for(item.asset_type)
        if recipe is None:
            raise RepurposeGateError(f"no channel recipe for asset type {item.asset_type!r}")
        prior = db.latest_draft(self.deps.store, campaign_id, asset_id)
        blocks = gen.system_blocks(self.deps.config, self.deps.brand_rules)
        job = DerivativeJob(
            asset_id=asset_id, asset_type=item.asset_type,
            recipe=recipe, volume=max(item.volume, 1),
        )
        draft = self._generate_derivative(
            ctx, campaign_id, job, inventory, blocks,
            {"campaign_id": campaign_id}, folder=folder, slug=slug,
            instruction=instruction,
        )
        if prior is not None:
            draft = draft.model_copy(update={"rework_of_version": prior.version})
            db.save_draft(self.deps.store, draft)
        return ReworkOutcome(
            case_id=campaign_id, trace_id=ctx.trace_id, campaign_id=campaign_id,
            status="derivatives_staged", draft=draft,
            escalation_reasons=[] if draft.status == "staged" else ["selfcheck_failed"],
        )

    # ------------------------------------------------------------------ helpers

    def _stage_draft(self, ctx: RunContext, draft: StagedDraft, folder: str) -> StagedDraft:
        """L4 side effect: kill-switch guard, idempotent additive uploads, telemetry.
        Only self-check-passing drafts ever reach this method."""
        deps = self.deps
        kill_state, breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, deps.config.agent_id,
            deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            self._escalation_event(
                ctx, tier=2, reason_code="tool_failure",
                detail={"control_pause_reason": pause_reason},
                control={"kill_switch": kill_state, "rate_breaker": breaker_state},
            )
            return draft.model_copy(update={"status": "withheld"})
        try:
            with ctx.span("workspace-stage-draft", "api") as span:
                file_ref = self._upload_once(
                    ctx, f"{draft.campaign_id}:draft:{draft.asset_id}:v{draft.version}",
                    f"{folder}/drafts", draft.filename, docs.draft_docx(draft),
                )
                claim_map_ref = self._upload_once(
                    ctx, f"{draft.campaign_id}:claims:{draft.asset_id}:v{draft.version}",
                    f"{folder}/drafts",
                    docs.claim_map_filename(draft.filename),
                    docs.claim_map_json(draft),
                )
        except WorkspaceWriteError as exc:
            self._escalation_event(
                ctx, tier=3, reason_code="workspace_failure",
                detail={"error": str(exc)},
                routed_override=deps.config.route_for("workspace_failure"),
            )
            raise
        staged = draft.model_copy(update={"file_ref": file_ref, "claim_map_ref": claim_map_ref})
        self._emit(
            ctx, "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "stage_draft",
                "shiftai.action.idempotency_key":
                    f"{draft.campaign_id}:draft:{draft.asset_id}:v{draft.version}",
                "shiftai.action.external_ref": file_ref,
                "shiftai.control.kill_switch": kill_state,
                "shiftai.control.rate_breaker": breaker_state,
                "shiftai.span.id": span.span_id,
                "shiftai.span.duration_ms": span.duration_ms,
                "shiftai.draft.asset_id": draft.asset_id,
                "shiftai.draft.version": draft.version,
                "shiftai.draft.claim_lineage": json.dumps(draft.claim_lineage),
            },
        )
        deps.rate_breaker.record_execution(deps.config.agent_id)
        return staged

    def _upload_once(
        self, ctx: RunContext, key: str, folder: str, filename: str, content: bytes
    ) -> str:
        def side_effect() -> dict[str, Any]:
            ref = self.deps.workspace.upload(folder, filename, content)
            return {"ref": ref}

        result, _ = execute_idempotent(key, self.deps.idempotency, side_effect)
        return str(result["ref"])

    def _load_checklist(self, campaign_id: str) -> AssetChecklist:
        record = self.deps.store.get(box_db.KIND_CHECKLIST, campaign_id)
        if record is None:
            raise RepurposeGateError(f"no asset checklist exists for {campaign_id!r}")
        return AssetChecklist.model_validate(record.value)

    def _next_version(self, campaign_id: str, asset_id: str) -> int:
        prior = db.latest_draft(self.deps.store, campaign_id, asset_id)
        return (prior.version + 1) if prior else 1

    def _save_case(
        self, campaign_id: str, ctx: RunContext, *, status: RepurposeStatus,
        extra: dict[str, Any],
    ) -> None:
        existing = db.load_case(self.deps.store, campaign_id) or {}
        db.save_case(self.deps.store, campaign_id, {
            **existing, "campaign_id": campaign_id, "trace_id": ctx.trace_id,
            "status": status, "run_cost_usd": ctx.total_cost_usd, **extra,
        })

    def _load_case_or_raise(self, campaign_id: str, expected: set[str]) -> dict[str, Any]:
        case = db.load_case(self.deps.store, campaign_id)
        if case is None:
            raise RepurposeGateError(f"unknown repurpose case {campaign_id!r}")
        if case.get("status") not in expected:
            raise RepurposeGateError(
                f"campaign {campaign_id!r} is {case.get('status')!r}; "
                f"expected one of {sorted(expected)}"
            )
        return case

    def _fail[T: (FlagshipOutcome, FanoutOutcome)](
        self, ctx: RunContext, campaign_id: str, exc: Exception, outcome_type: type[T]
    ) -> T:
        error_type = type(exc).__name__
        try:
            db.save_failed_run(self.deps.store, campaign_id, error_type, str(exc))
            existing = db.load_case(self.deps.store, campaign_id) or {}
            db.save_case(self.deps.store, campaign_id, {
                **existing, "campaign_id": campaign_id, "trace_id": ctx.trace_id,
                "status": "failed", "error_type": error_type,
            })
        except Exception:
            pass  # store down: telemetry below is the surviving record
        self._emit(ctx, "error", **{"error.type": error_type, "shiftai.outcome": "failure"})
        self._emit(
            ctx, "run_summary",
            **{"shiftai.outcome": "failure", "error.type": error_type,
               **self._latency_attrs(ctx)},
        )
        return outcome_type(
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


def inventory_pack_note(store: ContextStore, campaign_id: str) -> str:
    record = store.get(box_db.KIND_PACK, campaign_id)
    if record is None:
        return ""
    return str(record.value.get("value_proposition", ""))


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _digits(token: str) -> str:
    return "".join(ch for ch in token if ch.isdigit() or ch == ".")


def _elapsed_ms(since_iso: str) -> int:
    try:
        since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(int((datetime.now(tz=UTC) - since).total_seconds() * 1000), 0)
