"""Dev bindings for Agent 4's outbound signals (Execution Studio routes these in
production). This is where the old bridge stand-ins moved: confirmation now flows
Agent 4 → neighbors, with the REAL staged bytes and claim lineage."""

from __future__ import annotations

from typing import Any

from c2c_campaign_box import persistence as box_db
from c2c_content_repurposing import persistence as rp_db
from shiftai_shared.m365.word import DocSection, DocSpec, build_docx


def _fallback_docx(asset_id: str) -> bytes:
    """Only for assets Agent 3 never drafts (reuse decisions): the repository
    asset is reused as-is in production; dev registers a labeled placeholder."""
    return build_docx(
        DocSpec(
            title=f"Confirmed asset: {asset_id}",
            subtitle="Reuse asset — the repository version is used as-is (dev placeholder)",
            sections=(DocSection(heading="Content",
                                 paragraphs=("Reused from the content repository.",)),),
        )
    )


class BridgeSignals:
    """Binds Agent 4's Signals protocol to the co-hosted agents. ``bridge`` is the
    zero-arg accessor returning the live Bridge instance."""

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    # ---- flagship content_confirmed → unlock Agent 3's fan-out + register bytes

    def flagship_confirmed(self, campaign_id: str, actor_id: str, actor_role: str) -> None:
        import contextlib

        from c2c_content_repurposing.orchestration import RepurposeGateError

        bridge = self._bridge()
        flagship_id = bridge.repurposer.deps.config.flagship_asset_type
        # No Agent 3 flagship case (asset produced outside the drafting pipeline)
        # is fine — the packaging registration below still proceeds.
        with contextlib.suppress(RepurposeGateError):
            bridge.repurposer.confirm_flagship(
                campaign_id, actor_id=actor_id, actor_role=actor_role
            )
        self._register(campaign_id, flagship_id, actor_id, actor_role)

    # ---- derivative content_confirmed → packaging registry with REAL bytes

    def register_confirmed(
        self, campaign_id: str, asset_id: str, actor_id: str, actor_role: str
    ) -> None:
        self._register(campaign_id, asset_id, actor_id, actor_role)

    def _register(
        self, campaign_id: str, asset_id: str, actor_id: str, actor_role: str
    ) -> None:
        bridge = self._bridge()
        store = bridge.store
        case = box_db.load_plan_case(store, campaign_id) or {}
        slug = str(case.get("campaign_slug", "campaign"))
        prior = [
            a.version for a in box_db.load_registered_assets(store, campaign_id)
            if a.asset_id == asset_id
        ]
        staged = rp_db.latest_draft(store, campaign_id, asset_id)
        draft_version = 0
        claim_refs: list[str] = []
        if staged is not None and staged.status == "withheld":
            # Never package a draft the self-check refused — and never mask it
            # with a placeholder. The confirm routes block this earlier; this is
            # defense-in-depth (the raise lands as a tool_failure escalation).
            reasons = ", ".join(
                f["rule_id"] for f in staged.self_check.findings
            ) or "see draft card"
            raise RuntimeError(
                f"latest draft of {asset_id!r} was WITHHELD by self-check "
                f"({reasons}) — rework the asset before confirmation"
            )
        if staged is not None and staged.status == "staged" and staged.file_ref:
            content = bridge.repurposer.deps.workspace.download(staged.file_ref)
            draft_version = staged.version
            claim_refs = staged.claim_lineage or [m.source_ref for m in staged.claim_markers]
        else:
            # No draft chain at all: a genuine reuse-decision asset — the
            # repository version is used as-is in production.
            content = _fallback_docx(asset_id)
        # Version past both prior registrations and the draft chain — the
        # canonical filename never collides in the drafts folder.
        version = max([*prior, draft_version, 0]) + 1
        filename = f"{slug}-{asset_id.replace('_', '-')}-v{version}.docx"
        bridge.box.register_confirmed_asset(
            campaign_id, asset_id,
            filename=filename, content=content,
            actor_id=actor_id, actor_role=actor_role,
            claim_refs=claim_refs, version=version,
        )

    # ---- structural feedback → ONE consolidated rework instruction to Agent 3

    def route_rework(
        self, campaign_id: str, asset_id: str, instruction: str, actor_id: str
    ) -> None:
        self._bridge().repurposer.apply_rework(
            campaign_id, asset_id, instruction=instruction, actor_id=actor_id
        )


class GateSignals:
    """Binds Agent 5's Signals protocol to the co-hosted agents (Execution Studio
    routes these in production). ``bridge`` is the zero-arg accessor returning the
    live Bridge instance."""

    GATE_ACTOR = "quality_gate_approval"

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def assets_failed(
        self, campaign_id: str, findings_by_asset: dict[str, list[str]]
    ) -> None:
        """Failed assets return to the Collaboration Agent WITH findings, and the
        packaging case re-opens for exactly those assets so the reworked versions
        can re-register (spec step 12: re-entry at packaging)."""
        bridge = self._bridge()
        asset_ids = sorted(findings_by_asset)
        bridge.box.reopen_assets(
            campaign_id, asset_ids,
            requesting_gate="quality-gate", actor_id=self.GATE_ACTOR,
            notes="gate findings — see compliance report",
        )
        for asset_id in asset_ids:
            bridge.collab.reopen_review(
                campaign_id, asset_id,
                reason="gate_findings", actor_id=self.GATE_ACTOR,
            )
            for line in findings_by_asset[asset_id]:
                bridge.collab.add_feedback(
                    campaign_id, asset_id,
                    reviewer_id=self.GATE_ACTOR, reviewer_role="quality-gate",
                    section="", text=line,
                )

    def package_returned(
        self, campaign_id: str, asset_ids: list[str], notes: str, actor_id: str
    ) -> None:
        """A human review return: reviewer notes travel VERBATIM into the re-opened
        review cycle and the packaging case re-opens for the named assets."""
        bridge = self._bridge()
        bridge.box.reopen_assets(
            campaign_id, asset_ids,
            requesting_gate="approval-review", actor_id=actor_id,
            actor_role="content-reviewer", notes=notes,
        )
        for asset_id in asset_ids:
            bridge.collab.reopen_review(
                campaign_id, asset_id,
                reason="approval_return", actor_id=actor_id,
                actor_role="content-reviewer",
            )
            bridge.collab.add_feedback(
                campaign_id, asset_id,
                reviewer_id=actor_id, reviewer_role="content-reviewer",
                section="", text=notes or "Returned from approval review.",
            )

    def package_approved(self, campaign_id: str, manifest_version: int) -> None:
        """Phase 1's terminal hand-off: the locked package reference is persisted
        by the agent itself; Launch & Publishing (Phase 2) picks it up from the
        Context Store. Nothing is published from here — deliberately a no-op."""
        return None
