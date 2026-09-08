"""The Quality Gate & Approval state machine — Phase 1's terminal agent.

Per package: manifest hand-off → per-asset gate (deterministic + contextual,
fail-closed) → failed assets return to the Collaboration Agent with findings /
passing assets enter the sequenced human review (Grammar QA → BU Lead package
sign-off) → every approval recorded with identity + hash → approved versions
locked read-only → the locked package reference is the hand-off to Phase 2.

Structural design points:
- ``Signals`` decouples the agent from its neighbors (bridge binds in dev,
  Execution Studio in prod): gate failures → Collaboration re-open with findings;
  review returns → packaging re-open with reviewer notes verbatim; full approval
  → the Phase 2 hand-off reference. The gate itself never edits content and
  never publishes.
- Approvals exist ONLY inside ``record_review_outcome(actor_id, …)`` — no code
  path in this package invokes it (static-tested). An approval without a human
  identity is impossible by construction (spec alert: zero).
- Sequence integrity is structural (``routing.ordered_gate_open``): no gate can
  be skipped or reordered by any caller, for any urgency level.
- Fail-closed everywhere: hash mismatch, unreadable snapshot, contextual-pass
  error or an unfinished contextual rule all yield verdict=fail; deterministic
  findings stand even when the contextual pass errors (partial report, still
  blocking); a package never releases with an incomplete approval chain; a lock
  failure blocks release.
"""

from __future__ import annotations

import io
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import PackagedAsset, PackageManifest
from c2c_campaign_box.workspace import CampaignWorkspace
from c2c_content_repurposing import persistence as rp_db
from shiftai_shared.brand import BrandRules
from shiftai_shared.config import SharedSettings, runtime_rate_card
from shiftai_shared.context_store.store import ContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.llm import LLMProvider, LLMResponse
from shiftai_shared.resilience import IdempotencyStore
from shiftai_shared.telemetry import StsEmitter, TelemetrySink
from shiftai_shared.telemetry.envelope import RunContext, new_id, response_cost

from c2c_quality_gate import (
    AGENT_TYPE,
    ASSET_TIMEOUT_S,
    DATA_CLASSIFICATION,
    MODEL_ID,
    PROCESS_NAME,
    RISK_TIER,
    SYSTEM_PROMPT_VERSION,
)
from c2c_quality_gate import generation as gen
from c2c_quality_gate import persistence as db
from c2c_quality_gate.agent_config import QualityGateConfig
from c2c_quality_gate.deterministic import run_deterministic
from c2c_quality_gate.grounding import ground_contextual, verdict_for
from c2c_quality_gate.locking import LockError, check_locks, lock_assets, sha256_hex
from c2c_quality_gate.models import (
    ApprovalRecord,
    AssetReport,
    CalibrationEvent,
    Finding,
    GateOutcome,
    PackageGateState,
    ReviewTask,
    SweepOutcome,
    Verdict,
)
from c2c_quality_gate.routing import (
    build_asset_tasks,
    build_package_task,
    ordered_gate_open,
    plan_reminders,
)


class GateStateError(Exception):
    """Wrong package state for the requested action (no manifest, locked, …)."""


class GateSequencingError(Exception):
    """A structural violation: skipped/reordered gate or missing human identity."""


class Signals(Protocol):
    """Outbound signals (spec Connections). Bound by the bridge in dev,
    Execution Studio in production — the agent never imports its neighbors'
    orchestrators."""

    def assets_failed(
        self, campaign_id: str, findings_by_asset: dict[str, list[str]]
    ) -> None: ...

    def package_returned(
        self, campaign_id: str, asset_ids: list[str], notes: str, actor_id: str
    ) -> None: ...

    def package_approved(self, campaign_id: str, manifest_version: int) -> None: ...


@dataclass
class QualityGateDeps:
    provider: LLMProvider
    store: ContextStore
    workspace: CampaignWorkspace  # the SAME campaign workspace agents 2-4 use
    sink: TelemetrySink
    kill_switch: KillSwitch
    rate_breaker: RateBreaker
    idempotency: IdempotencyStore
    config: QualityGateConfig
    settings: SharedSettings
    brand_rules: BrandRules
    signals: Signals


class QualityGateAgent:
    def __init__(self, deps: QualityGateDeps) -> None:
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

    # ---------------------------------------------------------- steps 1-6: gate

    def run_gate(self, campaign_id: str) -> GateOutcome:
        """Gate the campaign's package manifest: per-asset deterministic +
        contextual checks, fail-closed. Idempotent per (asset, hash, config):
        unchanged assets reuse their report — re-running after rework IS the
        delta-focused re-check (spec step 12)."""
        deps = self.deps
        manifest = self._manifest_or_raise(campaign_id)
        state = db.load_gate_state(deps.store, campaign_id)
        if state is not None and state.status == "approved_locked":
            raise GateStateError(
                "package is approved and locked — changes re-enter at packaging"
            )
        ctx = self._ctx(campaign_id)
        kill_state, _breaker_state, pause_reason = guard_layer4(
            deps.kill_switch, deps.rate_breaker, deps.config.agent_id,
            deps.settings.shiftai_tenant_id,
        )
        if kill_state == "paused":
            raise GateStateError(f"control plane paused: {pause_reason}")

        reports: list[AssetReport] = []
        rechecked: set[str] = set()
        reused = 0
        for asset in manifest.assets:
            existing = db.load_report(deps.store, campaign_id, asset.asset_id)
            if (
                existing is not None
                and existing.sha256 == asset.sha256
                and existing.config_version == deps.config.version
                and existing.rules_pack_version == deps.brand_rules.version
                and existing.checks_complete
            ):
                reports.append(existing)
                reused += 1
                continue
            report = self._check_asset(ctx, campaign_id, asset)
            db.save_report(deps.store, report)
            reports.append(report)
            rechecked.add(asset.asset_id)

        failed = [r.asset_id for r in reports if r.verdict == "fail"]
        passed = [r.asset_id for r in reports if r.verdict == "pass"]
        tasks_created = self._reconcile_tasks(
            campaign_id, manifest, passed=passed, failed=failed, rechecked=rechecked
        )

        status: Any = "gate_failed" if failed else "in_review"
        now = db.now_iso()
        state = PackageGateState(
            campaign_id=campaign_id,
            manifest_id=manifest.manifest_id,
            manifest_version=manifest.version,
            status=status,
            asset_verdicts={r.asset_id: r.verdict for r in reports},
            failed_asset_ids=failed,
            locks=state.locks if state else [],
            gate_started_at=(state.gate_started_at if state and state.gate_started_at else now),
            slip_escalated=state.slip_escalated if state else False,
            updated_at=now,
        )
        db.save_gate_state(deps.store, state)

        if failed:
            findings_by_asset = {
                r.asset_id: [
                    f"[{f.rule_id}] {f.remediation or f.reasoning}"
                    + (f' (quote: "{f.quote}")' if f.quote else "")
                    for f in r.blocking
                ]
                or [f"checks incomplete: {r.incomplete_reason}"]
                for r in reports
                if r.verdict == "fail"
            }
            try:
                deps.signals.assets_failed(campaign_id, findings_by_asset)
            except Exception as exc:
                self._escalation_event(
                    ctx, tier=2, reason_code="tool_failure",
                    detail={"signal": "assets_failed", "error": str(exc),
                            "note": "gate verdicts recorded; rework signal failed — retry"},
                )
            self._escalation_event(
                ctx, tier=1, reason_code="gate_failed",
                detail={"failed_assets": failed},
            )

        self._emit(
            ctx, "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "gate_package",
                "shiftai.action.idempotency_key":
                    f"{campaign_id}:gate:m{manifest.version}",
                "shiftai.gate.verdicts": json.dumps(
                    {r.asset_id: r.verdict for r in reports}
                ),
                "shiftai.gate.reused_reports": reused,
                "shiftai.business_object.type": "campaign_package",
                "shiftai.business_object.id": f"{campaign_id}:m{manifest.version}",
            },
        )
        return GateOutcome(
            campaign_id=campaign_id,
            manifest_version=manifest.version,
            status=state.status,
            reports=reports,
            failed_asset_ids=failed,
            tasks_created=tasks_created,
            reused_reports=reused,
        )

    def _check_asset(
        self, ctx: RunContext, campaign_id: str, asset: PackagedAsset
    ) -> AssetReport:
        deps = self.deps
        base: dict[str, Any] = {
            "campaign_id": campaign_id,
            "asset_id": asset.asset_id,
            "asset_type": asset.asset_type,
            "version": asset.version,
            "sha256": asset.sha256,
            "rules_pack_id": deps.brand_rules.rules_pack_id,
            "rules_pack_version": deps.brand_rules.version,
            "config_version": deps.config.version,
            "checked_at": db.now_iso(),
        }
        # Step 1: resolve the final version by content hash — reject on mismatch.
        try:
            content = deps.workspace.download(asset.snapshot_ref)
        except OSError as exc:
            self._escalation_event(
                ctx, tier=2, reason_code="workspace_failure",
                detail={"asset_id": asset.asset_id, "error": str(exc)},
            )
            return AssetReport(
                **base, findings=[], verdict="fail", checks_complete=False,
                incomplete_reason=f"snapshot unreadable: {exc}",
            )
        if sha256_hex(content) != asset.sha256:
            self._escalation_event(
                ctx, tier=2, reason_code="hash_mismatch",
                detail={"asset_id": asset.asset_id,
                        "note": "manifest hash does not match snapshot bytes"},
            )
            return AssetReport(
                **base,
                findings=[Finding(
                    rule_id="hash_mismatch", severity="blocking",
                    source="deterministic", location="whole asset",
                    reasoning="Snapshot bytes do not match the manifest hash.",
                    remediation="Re-run packaging to restore chain of custody.",
                )],
                verdict="fail",
            )

        text = _extract_text(content)
        draft = rp_db.latest_draft(deps.store, campaign_id, asset.asset_id)
        # Resolvable claim references: the draft's own [c-N] markers PLUS the
        # [cl-N] lineage ids it cites from the confirmed flagship's verified
        # claim inventory (spec step 4: "proof points or claim inventory").
        resolved: set[str] = set()
        verified_claims: list[str] = []
        if draft is not None:
            resolved = {m.marker for m in draft.claim_markers} | set(draft.claim_lineage)
            # The verified claim set the asset is allowed to draw from: the
            # flagship's own markers, plus the confirmed claim inventory the
            # derivatives were built from. Reader-facing derivatives carry the
            # NUMBER without an inline marker, so the gate verifies by value
            # against this set, not by demanding [c-N] clutter in final copy.
            verified_claims = [f"{m.claim} (source: {m.source_ref})" for m in draft.claim_markers]
            inv_version = getattr(draft, "inventory_version", 0) or draft.version
            inventory = rp_db.load_inventory(deps.store, campaign_id, inv_version)
            if inventory is not None:
                verified_claims += [
                    f"[{i.claim_id}] {i.text}".strip() for i in inventory.items
                ]

        det_start = time.monotonic()
        findings = run_deterministic(
            text,
            asset_type=asset.asset_type,
            filename=asset.canonical_name,
            canonical_name=asset.canonical_name,
            resolved_markers=resolved,
            rules=deps.brand_rules,
            config=deps.config,
        )
        det_ms = int((time.monotonic() - det_start) * 1000)

        ctx_ms = 0
        checks_complete = True
        incomplete_reason = ""
        span_id = new_id("span")
        llm_start = time.monotonic()
        try:
            output, response = gen.run_contextual_call(
                deps.provider,
                gen.system_blocks(deps.brand_rules),
                gen.contextual_user_prompt(
                    asset_id=asset.asset_id, asset_type=asset.asset_type,
                    text=text, resolved_markers=sorted(resolved),
                    verified_claims=verified_claims,
                ),
                timeout_s=ASSET_TIMEOUT_S,
            )
            ctx_ms = int((time.monotonic() - llm_start) * 1000)
            ctx_findings, checks_complete, incomplete_reason, dropped = ground_contextual(
                output, text
            )
            findings.extend(ctx_findings)
            self._emit_l3(
                ctx, span_id, ctx_ms, response,
                action="contextual_compliance_check",
                confidence=output.confidence if output else 0.0,
                extra={
                    "shiftai.gate.asset_id": asset.asset_id,
                    "shiftai.gate.dropped_unknown_rules": dropped,
                },
            )
        except Exception as exc:
            ctx_ms = int((time.monotonic() - llm_start) * 1000)
            checks_complete = False
            incomplete_reason = f"contextual pass error: {exc}"
            self._escalation_event(
                ctx, tier=2, reason_code="checks_incomplete",
                detail={"asset_id": asset.asset_id, "error": str(exc),
                        "note": "fail-closed: deterministic findings stand, asset blocked"},
            )

        verdict: Verdict = "fail" if verdict_for(findings, checks_complete) == "fail" else "pass"
        return AssetReport(
            **base,
            findings=findings,
            verdict=verdict,
            checks_complete=checks_complete,
            incomplete_reason=incomplete_reason,
            deterministic_ms=det_ms,
            contextual_ms=ctx_ms,
        )

    def _reconcile_tasks(
        self,
        campaign_id: str,
        manifest: PackageManifest,
        *,
        passed: list[str],
        failed: list[str],
        rechecked: set[str],
    ) -> int:
        """Cancel stale tasks for re-checked assets; create sequences for passing
        assets that lack one; ensure exactly one live package sign-off task once
        every asset passes."""
        deps = self.deps
        tasks = db.load_tasks(deps.store, campaign_id)
        for task in tasks:
            stale = task.scope == "asset" and task.asset_id in rechecked
            if stale and task.status == "open":
                db.save_task(deps.store, task.model_copy(update={"status": "cancelled"}))
        tasks = db.load_tasks(deps.store, campaign_id)
        have_live_sequence = {
            t.asset_id for t in tasks
            if t.scope == "asset" and t.status in ("open", "approved")
        }
        need = [a for a in passed if a not in have_live_sequence]
        created = build_asset_tasks(
            deps.config, manifest, need, today=self._today(), created_at=db.now_iso()
        )
        for task in created:
            db.save_task(deps.store, task)
        count = len(created)
        if not failed:
            live_package = [
                t for t in tasks if t.scope == "package" and t.status in ("open", "approved")
            ]
            if not live_package:
                package_task = build_package_task(
                    deps.config, campaign_id, today=self._today(), created_at=db.now_iso()
                )
                db.save_task(deps.store, package_task)
                count += 1
        return count

    # ------------------------------------------- steps 9-10: human review (in)

    def record_review_outcome(
        self,
        campaign_id: str,
        task_id: str,
        *,
        decision: str,
        actor_id: str,
        actor_role: str,
        notes: str = "",
        disputed_rule_ids: list[str] | None = None,
        return_asset_ids: list[str] | None = None,
    ) -> ReviewTask:
        """A HUMAN review decision, identity-stamped. This method is never invoked
        by agent code (static-tested); the bridge/Execution Studio carries the
        reviewer's decision into it. Approving the package sign-off task locks the
        package — a deterministic consequence of that human decision."""
        deps = self.deps
        ctx = self._ctx(campaign_id)
        if not actor_id.strip():
            self._escalation_event(
                ctx, tier=2, reason_code="identity_missing",
                detail={"task_id": task_id},
            )
            raise GateSequencingError("a review decision requires a human actor identity")
        if decision not in ("approved", "returned"):
            raise GateSequencingError(f"unknown decision {decision!r}")
        task = db.load_task(deps.store, campaign_id, task_id)
        if task is None:
            raise GateStateError(f"unknown review task {task_id!r}")
        all_tasks = db.load_tasks(deps.store, campaign_id)
        violation = ordered_gate_open(task, all_tasks)
        if violation:
            self._escalation_event(
                ctx, tier=2, reason_code="sequence_violation",
                detail={"task_id": task_id, "violation": violation},
            )
            raise GateSequencingError(violation)
        if task.scope == "package" and decision == "returned" and not return_asset_ids:
            raise GateStateError(
                "a package return must name the assets to re-open (return_asset_ids)"
            )

        manifest = self._manifest_or_raise(campaign_id)
        by_id = {a.asset_id: a for a in manifest.assets}
        asset = by_id.get(task.asset_id)
        package_sha = sha256_hex("".join(a.sha256 for a in manifest.assets).encode())
        now = db.now_iso()
        approval = ApprovalRecord(
            approval_id=f"ap-{uuid.uuid4().hex[:8]}",
            campaign_id=campaign_id,
            scope=task.scope,
            asset_id=task.asset_id,
            step=task.step,
            decision="approved" if decision == "approved" else "returned",
            actor_id=actor_id,
            actor_role=actor_role,
            asset_version=asset.version if asset else manifest.version,
            sha256=asset.sha256 if asset else package_sha,
            notes=notes,
            at=now,
        )
        db.save_approval(deps.store, approval)
        decided = task.model_copy(update={
            "status": approval.decision,
            "decided_by": actor_id,
            "decided_role": actor_role,
            "decided_at": now,
            "notes": notes,
        })
        db.save_task(deps.store, decided)
        for rule_id in disputed_rule_ids or []:
            self._save_calibration(
                campaign_id, task.asset_id, rule_id, "false_positive",
                actor_id, actor_role, notes,
            )
        self._emit(
            ctx, "human_gate",
            **{
                "shiftai.hitl.decision": (
                    "approved" if decision == "approved" else "modified"
                ),
                "shiftai.hitl.actor.role": actor_role,
                "shiftai.learn.reason_code": (
                    "none" if decision == "approved" else "gate_failed"
                ),
                "shiftai.learn.agent_recommendation": f"review_step:{task.step}",
                "shiftai.learn.human_action": f"{decision}:{task.task_id}",
                "shiftai.business_object.type": (
                    "campaign_package" if task.scope == "package" else "campaign_asset"
                ),
                "shiftai.business_object.id": (
                    f"{campaign_id}:m{manifest.version}" if task.scope == "package"
                    else f"{campaign_id}:{task.asset_id}"
                ),
            },
        )

        if decision == "returned":
            asset_ids = (
                list(return_asset_ids or []) if task.scope == "package" else [task.asset_id]
            )
            state = self._state_or_raise(campaign_id)
            db.save_gate_state(deps.store, state.model_copy(update={
                "status": "returned", "updated_at": now,
            }))
            for other in db.load_tasks(deps.store, campaign_id):
                if (
                    other.scope == "asset" and other.asset_id in asset_ids
                    and other.status == "open"
                ):
                    db.save_task(
                        deps.store, other.model_copy(update={"status": "cancelled"})
                    )
            try:
                deps.signals.package_returned(campaign_id, asset_ids, notes, actor_id)
            except Exception as exc:
                self._escalation_event(
                    ctx, tier=2, reason_code="tool_failure",
                    detail={"signal": "package_returned", "error": str(exc),
                            "note": "return recorded; re-open signal failed — retry"},
                )
            return decided

        if task.scope == "package":
            self._finalize(ctx, campaign_id, manifest)
        return decided

    def _finalize(
        self, ctx: RunContext, campaign_id: str, manifest: PackageManifest
    ) -> None:
        """After the BU Lead sign-off: verify the chain is complete, lock every
        approved version, stamp the package approved, hand off the reference."""
        deps = self.deps
        tasks = db.load_tasks(deps.store, campaign_id)
        unapproved = [
            t for t in tasks if t.scope == "asset" and t.status not in ("approved", "cancelled")
        ]
        if unapproved:  # structurally unreachable past ordered_gate_open; belt+braces
            raise GateSequencingError(
                f"approval chain incomplete: {[t.task_id for t in unapproved]}"
            )
        try:
            locks = lock_assets(deps.workspace, manifest)
        except LockError as exc:
            self._escalation_event(
                ctx, tier=3, reason_code="lock_failure",
                detail={"error": str(exc), "note": "release blocked until resolved"},
            )
            raise GateStateError(f"lock failed — package not released: {exc}") from exc
        now = db.now_iso()
        state = self._state_or_raise(campaign_id)
        db.save_gate_state(deps.store, state.model_copy(update={
            "status": "approved_locked",
            "locks": locks,
            "approved_at": now,
            "updated_at": now,
        }))
        try:
            deps.signals.package_approved(campaign_id, manifest.version)
        except Exception as exc:
            self._escalation_event(
                ctx, tier=2, reason_code="tool_failure",
                detail={"signal": "package_approved", "error": str(exc)},
            )
        self._emit(
            ctx, "action_taken",
            **{
                "shiftai.layer": "L4",
                "shiftai.action.class": "lock_and_release_package",
                "shiftai.action.idempotency_key":
                    f"{campaign_id}:lock:m{manifest.version}",
                "shiftai.gate.locked_assets": len(locks),
                "shiftai.business_object.type": "campaign_package",
                "shiftai.business_object.id": f"{campaign_id}:m{manifest.version}",
            },
        )

    # -------------------------------------------------- step 11: calibration

    def record_false_negative(
        self,
        campaign_id: str,
        asset_id: str,
        *,
        rule_id: str,
        reviewer_id: str,
        reviewer_role: str,
        notes: str = "",
    ) -> CalibrationEvent:
        """A reviewer caught a violation the gate missed — labeled example plus an
        immediate rules-review alert (spec Alerting)."""
        if not reviewer_id.strip():
            raise GateSequencingError("calibration requires a reviewer identity")
        event = self._save_calibration(
            campaign_id, asset_id, rule_id, "false_negative",
            reviewer_id, reviewer_role, notes,
        )
        self._escalation_event(
            self._ctx(campaign_id), tier=2, reason_code="calibration_alert",
            detail={"asset_id": asset_id, "rule_id": rule_id,
                    "kind": "false_negative", "notes": notes},
        )
        return event

    def _save_calibration(
        self,
        campaign_id: str,
        asset_id: str,
        rule_id: str,
        kind: str,
        reviewer_id: str,
        reviewer_role: str,
        notes: str,
    ) -> CalibrationEvent:
        event = CalibrationEvent(
            event_id=f"cal-{uuid.uuid4().hex[:8]}",
            campaign_id=campaign_id,
            asset_id=asset_id,
            rule_id=rule_id,
            kind="false_negative" if kind == "false_negative" else (
                "dispute" if kind == "dispute" else "false_positive"
            ),
            reviewer_id=reviewer_id,
            reviewer_role=reviewer_role,
            notes=notes,
            at=db.now_iso(),
        )
        db.save_calibration(self.deps.store, event)
        return event

    # ---------------------------------------------------- step 9: SLA sweep

    def sweep(self, campaign_id: str, today: date | None = None) -> SweepOutcome:
        """Reminders at the configured SLA thresholds, escalation past due;
        package-level slips escalate to the BU Campaign Lead. Monotonic."""
        deps = self.deps
        today = today or self._today()
        ctx = self._ctx(campaign_id)
        tasks = db.load_tasks(deps.store, campaign_id)
        outcome = plan_reminders(tasks, deps.config, today)
        by_id = {t.task_id: t for t in tasks}
        for detail in outcome.details:
            action, task_id = detail.split(":", 1)
            task = by_id[task_id]
            if action == "escalate":
                db.save_task(deps.store, task.model_copy(update={"escalated": True}))
                self._escalation_event(
                    ctx, tier=1, reason_code="review_overdue",
                    detail={"task_id": task_id, "step": task.step, "role": task.role,
                            "due": task.due},
                )
            else:
                number = 2 if action == "remind2" else 1
                db.save_task(
                    deps.store, task.model_copy(update={"reminders_sent": number})
                )
                self._emit(
                    ctx, "action_taken",
                    **{
                        "shiftai.layer": "L4",
                        "shiftai.action.class": "send_review_reminder",
                        "shiftai.action.idempotency_key":
                            f"{campaign_id}:remind:{task_id}:{number}",
                        "shiftai.review.due": task.due,
                        "shiftai.business_object.type": "review_task",
                        "shiftai.business_object.id": task_id,
                    },
                )
        state = db.load_gate_state(deps.store, campaign_id)
        if (
            state is not None
            and state.status == "in_review"
            and not state.slip_escalated
            and state.gate_started_at
        ):
            from c2c_quality_gate.business_days import business_days_between

            started = date.fromisoformat(state.gate_started_at[:10])
            if business_days_between(started, today) > deps.config.package_sla_business_days:
                db.save_gate_state(deps.store, state.model_copy(update={
                    "slip_escalated": True, "updated_at": db.now_iso(),
                }))
                outcome.package_slips_escalated += 1
                self._escalation_event(
                    ctx, tier=2, reason_code="package_slip",
                    detail={"gate_started_at": state.gate_started_at,
                            "sla_business_days": deps.config.package_sla_business_days},
                )
        return outcome

    # -------------------------------------------- step 12: post-lock integrity

    def verify_locks(self, campaign_id: str) -> list[str]:
        """Hash-verify every locked version; any modification invalidates the
        package (re-entry happens at packaging, spec step 12)."""
        deps = self.deps
        state = db.load_gate_state(deps.store, campaign_id)
        if state is None or state.status != "approved_locked":
            return []
        violated = check_locks(deps.workspace, state.locks)
        if violated:
            db.save_gate_state(deps.store, state.model_copy(update={
                "status": "invalidated", "updated_at": db.now_iso(),
            }))
            self._escalation_event(
                self._ctx(campaign_id), tier=3, reason_code="post_lock_modification",
                detail={"assets": violated, "note": "package invalidated — halt"},
            )
        return violated

    # ------------------------------------------------------------- plumbing

    def _manifest_or_raise(self, campaign_id: str) -> PackageManifest:
        record = self.deps.store.get(box_db.KIND_MANIFEST, campaign_id)
        if record is None:
            raise GateStateError(
                f"no package manifest for {campaign_id!r} — run packaging first"
            )
        return PackageManifest.model_validate(record.value)

    def _state_or_raise(self, campaign_id: str) -> PackageGateState:
        state = db.load_gate_state(self.deps.store, campaign_id)
        if state is None:
            raise GateStateError(f"gate has not run for {campaign_id!r}")
        return state

    def _today(self) -> date:
        return datetime.now(tz=UTC).date()

    def _ctx(self, campaign_id: str) -> RunContext:
        case = box_db.load_plan_case(self.deps.store, campaign_id)
        trace = str(case.get("trace_id", "")) if case else ""
        return RunContext(case_id=campaign_id, trace_id=trace or new_id("trace"))

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
            "shiftai.prompt.template.id": gen.CONTEXTUAL_TEMPLATE_ID,
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

    def _emit(self, ctx: RunContext, event_type: str, **attrs: Any) -> None:
        self.emitter.emit(
            event_type,
            case_id=ctx.case_id,
            trace_id=ctx.trace_id,
            **{**ctx.run_attributes(), **attrs},
        )


def _extract_text(content: bytes) -> str:
    """Asset text for rule checks: docx paragraphs when the bytes are a Word
    document, utf-8 text otherwise (dev seeds / plain snapshots)."""
    try:
        from docx import Document

        document = Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception:
        return content.decode("utf-8", "replace")
