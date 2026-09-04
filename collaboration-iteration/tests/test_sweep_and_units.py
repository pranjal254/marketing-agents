"""Unit tests: sweep ladder timing (business days), marker shield, reconciliation."""

from __future__ import annotations

from datetime import date

from conftest import CAMPAIGN_ID, add_default_feedback
from shiftai_shared.context_store import InMemoryContextStore

from c2c_collaboration import persistence as db
from c2c_collaboration.agent_config import CollaborationConfig
from c2c_collaboration.grounding import protect_markers, reconcile_consolidation
from c2c_collaboration.models import (
    ConsolidationLLMOutput,
    FeedbackItem,
    NormalizedItem,
    ReviewState,
    RevisedSection,
)
from c2c_collaboration.orchestration import CollaborationAgent
from c2c_collaboration.sweep import business_days_between, plan_sweep

ASSET = "linkedin_posts"


def _state(due: str, *, reminders: int = 0, escalated: bool = False) -> ReviewState:
    return ReviewState(
        campaign_id="cmp", asset_id="a1", asset_type="linkedin_posts",
        review_gate="derivative", due=due, reminders_sent=reminders,
        escalated=escalated, staged_at="2026-10-01T00:00:00Z",
    )


def test_business_days_skip_weekends() -> None:
    friday, monday = date(2026, 10, 9), date(2026, 10, 12)
    assert business_days_between(friday, monday) == 1
    assert business_days_between(monday, monday) == 0


def test_sweep_ladder_first_second_escalate(config: CollaborationConfig) -> None:
    due = "2026-10-13"  # a Tuesday
    on_due = plan_sweep([_state(due)], config, date(2026, 10, 13))
    assert [a.action for a in on_due] == ["remind"] and on_due[0].reminder_number == 1
    second = plan_sweep([_state(due, reminders=1)], config, date(2026, 10, 14))
    assert [a.action for a in second] == ["remind"] and second[0].reminder_number == 2
    escalate = plan_sweep([_state(due, reminders=2)], config, date(2026, 10, 15))
    assert [a.action for a in escalate] == ["escalate"]
    assert escalate[0].blocking_roles == []  # default state has no reviewers listed


def test_sweep_is_monotonic_and_never_repeats(config: CollaborationConfig) -> None:
    due = "2026-10-13"
    already = plan_sweep([_state(due, reminders=2, escalated=True)],
                         config, date(2026, 10, 20))
    assert already == []
    not_due = plan_sweep([_state("2026-10-13")], config, date(2026, 10, 12))
    assert not_due == []


def test_agent_sweep_updates_state_and_emits(
    agent: CollaborationAgent, store: InMemoryContextStore,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)  # due 2026-10-13 from the plan
    outcome = agent.sweep(today=date(2026, 10, 13))
    assert outcome.reminded == [f"{CAMPAIGN_ID}:{ASSET}"]
    state = db.load_state(store, CAMPAIGN_ID, ASSET)
    assert state is not None and state.reminders_sent == 1
    outcome2 = agent.sweep(today=date(2026, 10, 16))  # ≥2bd overdue → escalate
    assert outcome2.escalated == [f"{CAMPAIGN_ID}:{ASSET}"]
    state = db.load_state(store, CAMPAIGN_ID, ASSET)
    assert state is not None and state.escalated is True
    assert agent.sweep(today=date(2026, 10, 20)).escalated == []  # never repeated


# ------------------------------------------------------------- marker shield


def _sections(*pairs: tuple[str, list[str]]) -> list[RevisedSection]:
    return [RevisedSection(heading=h, paragraphs=p) for h, p in pairs]


def test_shield_restores_reworded_marker_sentence() -> None:
    original = _sections(("Hook", ["Retailers report 42% faster cycles [c-1].", "Free line."]))
    revised = _sections(("Hook", ["Retailers report 50% faster cycles [c-1].", "Free line."]))
    safe, violations = protect_markers(original, revised)
    assert safe == original and violations


def test_shield_restores_dropped_marker_section_and_allows_free_edits() -> None:
    original = _sections(
        ("Hook", ["Stat [c-1] stays."]), ("CTA", ["Old call to action."])
    )
    revised = _sections(("CTA", ["New call to action."]))
    safe, violations = protect_markers(original, revised)
    assert [s.heading for s in safe] == ["Hook", "CTA"]
    assert safe[0].paragraphs == ["Stat [c-1] stays."]  # restored
    assert safe[1].paragraphs == ["New call to action."]  # free edit allowed
    assert violations == ["section dropped: Hook"]


def test_shield_passes_when_marker_sentences_survive() -> None:
    original = _sections(("Hook", ["Stat [c-1] stays. Editable tail."]))
    revised = _sections(("Hook", ["Stat [c-1] stays. A rewritten tail."]))
    safe, violations = protect_markers(original, revised)
    assert safe == revised and violations == []


# ------------------------------------------------------------- reconciliation


def test_reconcile_covers_input_exactly_and_ignores_hallucinations() -> None:
    items = [
        FeedbackItem(feedback_id="fb-1", campaign_id="c", asset_id="a",
                     reviewer_id="jen", reviewer_role="content-writer", text="one"),
        FeedbackItem(feedback_id="fb-2", campaign_id="c", asset_id="a",
                     reviewer_id="jen", reviewer_role="content-writer", text="two"),
    ]
    output = ConsolidationLLMOutput(items=[
        NormalizedItem(feedback_id="fb-1", instruction="one", type="textual"),
        NormalizedItem(feedback_id="fb-ghost", instruction="invented", type="textual"),
    ])
    normalized, unclassified = reconcile_consolidation(items, output)
    assert [n.feedback_id for n in normalized] == ["fb-1", "fb-2"]
    assert unclassified == ["fb-2"]


def test_feedback_visible_via_persistence_roundtrip(
    agent: CollaborationAgent, store: InMemoryContextStore,
) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    ids = add_default_feedback(agent)
    stored = db.open_feedback(store, CAMPAIGN_ID, ASSET)
    assert {i.feedback_id for i in stored} == set(ids.values())
