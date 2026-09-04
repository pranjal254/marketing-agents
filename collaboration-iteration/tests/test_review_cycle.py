"""End-to-end review-cycle tests: stage → feedback → round (consolidate, hold
conflicts, shield markers, route structural, stage revision) → resolve →
human-only confirm → signals. All mocked, no live calls."""

from __future__ import annotations

import pytest
from c2c_content_repurposing.persistence import load_drafts, save_draft
from conftest import (
    CAMPAIGN_ID,
    FOLDER,
    MARKER_SENTENCE,
    RecordingSignals,
    ScriptedReviewProvider,
    add_default_feedback,
    events_of,
    make_draft,
)
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.telemetry import InMemorySink

from c2c_collaboration import persistence as db
from c2c_collaboration.orchestration import (
    CollaborationAgent,
    ReviewGateError,
    VersionCorruptionError,
)

ASSET = "linkedin_posts"


# ---------------------------------------------------------------- step 1: stage


def test_stage_assigns_reviewers_and_due_from_plan(agent: CollaborationAgent) -> None:
    state = agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    assert state.review_gate == "derivative"
    assert [r.role for r in state.reviewers] == ["content-writer"]
    assert state.due == "2026-10-13"  # from the workflow plan's review_due
    flagship = agent.on_draft_staged(CAMPAIGN_ID, "flagship_blog")
    assert {r.role for r in flagship.reviewers} == {"content-writer", "marketing-lead"}
    assert flagship.review_gate == "flagship"


def test_stage_is_idempotent_per_version_and_refreshes_on_regen(
    agent: CollaborationAgent, store: InMemoryContextStore
) -> None:
    first = agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    again = agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    assert again == first
    save_draft(store, make_draft(ASSET, ASSET, version=2))
    refreshed = agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    assert refreshed.draft_version == 2 and refreshed.status == "in_review"


# ------------------------------------------------------------------- feedback


def test_feedback_requires_text_and_open_state(agent: CollaborationAgent) -> None:
    with pytest.raises(ReviewGateError):
        agent.add_feedback(CAMPAIGN_ID, ASSET, reviewer_id="jen",
                           reviewer_role="content-writer", text="   ")
    item = agent.add_feedback(CAMPAIGN_ID, ASSET, reviewer_id="jen",
                              reviewer_role="content-writer", text="Tighten the CTA")
    assert item.status == "open" and item.feedback_id.startswith("fb-")


# ---------------------------------------------------------------- the round


def test_round_covers_every_item_and_enforces_every_guardrail(
    agent: CollaborationAgent,
    store: InMemoryContextStore,
    signals: RecordingSignals,
    sink: InMemorySink,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    ids = add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")

    # Never dropped: every input item has exactly one resolution.
    assert outcome.round is not None
    resolved = {r.feedback_id: r.outcome for r in outcome.round.resolutions}
    assert set(resolved) == set(ids.values())

    # Conflicts held with BOTH positions quoted, never adjudicated.
    assert len(outcome.conflicts) == 1
    conflict = outcome.conflicts[0]
    quotes = {p.quote for p in conflict.positions}
    assert quotes == {"Make the hook casual", "Keep the hook formal"}
    assert resolved[ids["fb-casual"]] == "conflicted"
    assert resolved[ids["fb-formal"]] == "conflicted"
    assert outcome.status == "awaiting_conflict_resolution"

    # Structural routed as ONE consolidated instruction via the signal seam.
    routed = [c for c in signals.calls if c[0] == "route_rework"]
    assert len(routed) == 1 and "governance-first" in routed[0][1][2]
    assert resolved[ids["fb-structural"]] == "routed_structural"

    # Out-of-scope logged for the backlog, not acted on.
    assert resolved[ids["fb-oos"]] == "logged_backlog"

    # Marker shield: the model tried 42% → 50%; the Hook section is restored and
    # the edit flagged for a human — never applied.
    assert outcome.round.marker_violations
    assert resolved[ids["fb-marker"]] == "flagged_sourced_claim"
    drafts = [d for d in load_drafts(store, CAMPAIGN_ID) if d.asset_id == ASSET]
    revised = max(drafts, key=lambda d: d.version)
    assert revised.version == 2
    hook = next(s for s in revised.sections if s.heading == "Hook")
    assert MARKER_SENTENCE in " ".join(hook.paragraphs)  # restored verbatim

    # The safe CTA edit applied; revision staged as an additive new version.
    cta = next(s for s in revised.sections if s.heading == "CTA")
    assert cta.paragraphs == ["Talk to LevelShift today."]
    assert revised.rework_of_version == 1

    # Feedback items consumed (versioned update, never deleted).
    assert db.open_feedback(store, CAMPAIGN_ID, ASSET) == []

    # Telemetry: both L3 calls carry template identity; escalations carry codes.
    templates = {e["shiftai.prompt.template.id"] for e in events_of(sink, "decision_made")}
    assert templates == {"collaboration-consolidation", "collaboration-revision"}
    codes = {e["shiftai.learn.reason_code"] for e in events_of(sink, "case_escalated")}
    assert {"feedback_conflict", "sourced_claim_edit"} <= codes


def test_round_requires_open_feedback_and_a_draft(agent: CollaborationAgent) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    with pytest.raises(ReviewGateError, match="no open feedback"):
        agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    # Reuse assets have no staged draft — rounds refuse, confirm-direct guides.
    agent.add_feedback(CAMPAIGN_ID, "battle_card", reviewer_id="jen",
                       reviewer_role="content-writer", text="looks fine")
    with pytest.raises(ReviewGateError, match="no staged draft"):
        agent.run_review_round(CAMPAIGN_ID, "battle_card", actor_id="jen@x.com")


def test_unparsable_consolidation_defers_everything_never_drops(
    agent: CollaborationAgent, provider: ScriptedReviewProvider,
    store: InMemoryContextStore, sink: InMemorySink,
) -> None:
    provider.consolidation_reply = "this is not json"
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    ids = add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    assert outcome.round is not None
    outcomes = {r.outcome for r in outcome.round.resolutions}
    assert outcomes == {"deferred"}
    assert len(outcome.round.resolutions) == len(ids)
    codes = [e["shiftai.learn.reason_code"] for e in events_of(sink, "case_escalated")]
    assert "unclassified_feedback" in codes


def test_model_losing_an_item_yields_deferred_not_silence(
    agent: CollaborationAgent, provider: ScriptedReviewProvider,
) -> None:
    provider.drop_feedback_containing = "webinar"
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    ids = add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    assert outcome.round is not None
    resolved = {r.feedback_id: r.outcome for r in outcome.round.resolutions}
    assert resolved[ids["fb-oos"]] == "deferred"  # reconciled back in, visibly


def test_version_chain_corruption_halts_the_asset(
    agent: CollaborationAgent, store: InMemoryContextStore,
) -> None:
    save_draft(store, make_draft(ASSET, ASSET, version=4))  # gap: v1 then v4
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    agent.add_feedback(CAMPAIGN_ID, ASSET, reviewer_id="jen",
                       reviewer_role="content-writer", text="anything")
    with pytest.raises(VersionCorruptionError):
        agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")


# ------------------------------------------------------------- human gates


def test_conflict_resolution_carries_identity_and_feeds_next_round(
    agent: CollaborationAgent, store: InMemoryContextStore, sink: InMemorySink,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    conflict = outcome.conflicts[0]
    resolved = agent.resolve_conflict(
        CAMPAIGN_ID, ASSET, conflict.conflict_id,
        decision="Keep it formal — executive audience", actor_id="rishi@x.com",
    )
    assert resolved.resolution is not None
    assert resolved.resolution["actor_id"] == "rishi@x.com"
    # The decision becomes attributed feedback for the next round.
    reopened = db.open_feedback(store, CAMPAIGN_ID, ASSET)
    assert len(reopened) == 1 and "Keep it formal" in reopened[0].text
    state = db.load_state(store, CAMPAIGN_ID, ASSET)
    assert state is not None and state.status == "in_revision"
    with pytest.raises(ReviewGateError, match="already resolved"):
        agent.resolve_conflict(CAMPAIGN_ID, ASSET, conflict.conflict_id,
                               decision="again", actor_id="rishi@x.com")
    gates = events_of(sink, "human_gate")
    assert any(g["shiftai.learn.human_action"] == f"resolve_conflict:{conflict.conflict_id}"
               for g in gates)


def test_confirm_blocked_while_conflicts_open(agent: CollaborationAgent) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    add_default_feedback(agent)
    agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    with pytest.raises(ReviewGateError, match="unresolved reviewer conflicts"):
        agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="jen@x.com",
                              actor_role="content-writer")


def test_confirm_is_identity_stamped_signals_and_sets_aside_leftovers(
    agent: CollaborationAgent, store: InMemoryContextStore,
    signals: RecordingSignals, sink: InMemorySink,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    agent.resolve_conflict(CAMPAIGN_ID, ASSET, outcome.conflicts[0].conflict_id,
                           decision="Keep it formal", actor_id="rishi@x.com")
    # One open item remains (the resolution feedback) — the human sets it aside.
    state = agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="jen.cook@levelshift.com",
                                  actor_role="content-writer")
    assert state.status == "content_confirmed"
    assert state.confirmed_by == "jen.cook@levelshift.com"
    rounds = db.load_rounds(store, CAMPAIGN_ID, ASSET)
    assert rounds[-1].resolutions[-1].outcome == "rejected_by_human"
    assert db.open_feedback(store, CAMPAIGN_ID, ASSET) == []
    # Derivative confirmation signals packaging registration.
    assert ("register_confirmed",
            (CAMPAIGN_ID, ASSET, "jen.cook@levelshift.com", "content-writer")) in signals.calls
    # Metrics persisted (sub-process 5 raw material).
    metrics = store.get(db.KIND_METRICS, f"{CAMPAIGN_ID}:{ASSET}")
    assert metrics is not None and metrics.value["rounds"] == 1
    gates = events_of(sink, "human_gate")
    assert any(g["shiftai.learn.human_action"] == f"content_confirmed:{ASSET}" for g in gates)
    with pytest.raises(ReviewGateError, match="already content_confirmed"):
        agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="x", actor_role="y")


def test_flagship_confirm_signals_fanout_unlock(
    agent: CollaborationAgent, signals: RecordingSignals,
) -> None:
    agent.confirm_content(CAMPAIGN_ID, "flagship_blog",
                          actor_id="jen@x.com", actor_role="content-writer")
    assert signals.calls == [("flagship_confirmed",
                              (CAMPAIGN_ID, "jen@x.com", "content-writer"))]


def test_confirm_requires_identity_and_survives_signal_failure(
    agent: CollaborationAgent, signals: RecordingSignals, sink: InMemorySink,
) -> None:
    with pytest.raises(ReviewGateError, match="human actor identity"):
        agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="  ", actor_role="x")
    signals.raise_on.add("register_confirmed")
    state = agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="jen@x.com",
                                  actor_role="content-writer")
    assert state.status == "content_confirmed"  # the human decision stands
    codes = [e["shiftai.learn.reason_code"] for e in events_of(sink, "case_escalated")]
    assert "tool_failure" in codes


def test_reuse_asset_confirms_directly_without_a_draft(
    agent: CollaborationAgent, signals: RecordingSignals,
) -> None:
    state = agent.confirm_content(CAMPAIGN_ID, "battle_card",
                                  actor_id="jen@x.com", actor_role="content-writer")
    assert state.status == "content_confirmed" and state.rounds == 0
    assert signals.calls[-1][0] == "register_confirmed"


# ------------------------------------------------------------------- documents


def test_revision_doc_lands_in_workspace_with_edit_summary(
    agent: CollaborationAgent, workspace: object, store: InMemoryContextStore,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    add_default_feedback(agent)
    agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    from c2c_campaign_box.workspace import LocalCampaignWorkspace

    assert isinstance(workspace, LocalCampaignWorkspace)
    names = {f.name for f in workspace.list_files(f"{FOLDER}/drafts")}
    assert "review-test-linkedin-posts-v2.docx" in names
