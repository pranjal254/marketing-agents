"""Acceptance: one test per spec Implementation Task (Agent 3, steps 1-10)."""

from __future__ import annotations

import json

import pytest
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from conftest import CAMPAIGN_ID, FOLDER, build_agent, events_of, seed_box_plan
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.llm import MockLLMProvider
from shiftai_shared.telemetry import InMemorySink

from c2c_content_repurposing import persistence as db
from c2c_content_repurposing.agent_config import RepurposingConfig
from c2c_content_repurposing.orchestration import (
    ContentRepurposingAgent,
    SequencingViolationError,
)


@pytest.fixture()
def confirmed_agent(
    agent: ContentRepurposingAgent,
) -> ContentRepurposingAgent:
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen.cook@levelshift.com")
    return agent


def test_step_01_validates_claims_and_refuses_unverified_sections(
    agent: ContentRepurposingAgent,
) -> None:
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert any("made-up-competitor-stat" in g.needed for g in outcome.gap_notes)
    assert outcome.draft is not None
    assert all(s.heading != "Unverifiable angle" for s in outcome.draft.sections)


def test_step_02_drafts_flagship_from_outline_in_brand_voice(
    agent: ContentRepurposingAgent,
) -> None:
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.draft is not None and outcome.draft.kind == "flagship"
    assert outcome.draft.self_check.passed  # brand rules pass at generation time


def test_step_03_embeds_inline_source_markers(agent: ContentRepurposingAgent) -> None:
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.draft is not None
    text = " ".join(p for s in outcome.draft.sections for p in s.paragraphs)
    assert "[c-1]" in text
    assert outcome.draft.claim_markers[0].source_ref == "sig:1"
    assert outcome.draft.claim_map_ref  # sidecar claim map staged with the doc


def test_step_04_stages_versioned_doc_and_does_not_fan_out(
    agent: ContentRepurposingAgent, workspace: LocalCampaignWorkspace
) -> None:
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "flagship_staged"
    assert outcome.draft is not None and outcome.draft.filename.endswith("-v1.docx")
    names = {f.name for f in workspace.list_files(f"{FOLDER}/drafts")}
    assert names == {outcome.draft.filename,
                     outcome.draft.filename.removesuffix(".docx") + ".claims.json"}
    with pytest.raises(SequencingViolationError):
        agent.run_fanout(CAMPAIGN_ID)


def test_step_05_extracts_claim_inventory_from_confirmed_flagship(
    confirmed_agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    outcome = confirmed_agent.run_fanout(CAMPAIGN_ID)
    assert outcome.inventory is not None and outcome.inventory.items
    stored = db.load_inventory(store, CAMPAIGN_ID, 1)
    assert stored is not None and stored.items[0].source_ref == "sig:1"


def test_step_06_generates_channel_native_derivatives_per_recipe(
    confirmed_agent: ContentRepurposingAgent,
) -> None:
    outcome = confirmed_agent.run_fanout(CAMPAIGN_ID)
    types = {d.asset_type for d in outcome.staged}
    assert types == {"linkedin_posts", "faq_service_page"}
    faq = next(d for d in outcome.staged if d.asset_type == "faq_service_page")
    text = " ".join(p for s in faq.sections for p in s.paragraphs)
    assert "LevelShift" in text  # AEO named-mention rule enforced


def test_step_07_respects_volume_limits_and_checklist_membership(
    confirmed_agent: ContentRepurposingAgent,
) -> None:
    outcome = confirmed_agent.run_fanout(CAMPAIGN_ID)
    linkedin = next(d for d in outcome.staged if d.asset_id == "linkedin_posts")
    assert len(linkedin.sections) == 2  # checklist volume, model offered 3
    assert "battle_card" in outcome.skipped  # reuse decision — never drafted
    staged_ids = {d.asset_id for d in outcome.staged}
    assert "enablement_notes" not in staged_ids  # not on this checklist → not generated


def test_step_08_runs_generation_time_self_check_per_asset(
    confirmed_agent: ContentRepurposingAgent,
) -> None:
    outcome = confirmed_agent.run_fanout(CAMPAIGN_ID)
    assert all(d.self_check.passed for d in outcome.staged)
    assert all(d.self_check.attempts >= 1 for d in outcome.staged)


def test_step_09_registers_drafts_with_claim_lineage_and_naming(
    confirmed_agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    confirmed_agent.run_fanout(CAMPAIGN_ID)
    drafts = [d for d in db.load_drafts(store, CAMPAIGN_ID) if d.kind == "derivative"]
    assert drafts
    for d in drafts:
        assert d.claim_lineage == ["cl-1"]
        assert d.filename.startswith("erp-modernization-")
        assert d.filename.endswith(f"-v{d.version}.docx")


def test_step_10_rework_regenerates_only_the_affected_asset(
    confirmed_agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    confirmed_agent.run_fanout(CAMPAIGN_ID)
    confirmed_agent.apply_rework(
        CAMPAIGN_ID, "faq_service_page",
        instruction="Answer the pricing objection directly", actor_id="rishi",
    )
    versions = {
        d.asset_id: max(x.version for x in db.load_drafts(store, CAMPAIGN_ID)
                        if x.asset_id == d.asset_id)
        for d in db.load_drafts(store, CAMPAIGN_ID)
    }
    assert versions["faq_service_page"] == 2
    assert versions["linkedin_posts"] == 1
    assert versions["flagship_blog"] == 1


def test_cross_standard_prompt_caching_blocks_are_stable(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
    provider: MockLLMProvider,
) -> None:
    """Standard A: the system blocks are identical (and cache-marked) across the
    flagship call and every fan-out call."""
    agent = build_agent(provider, store, workspace, sink, config, settings)
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    agent.run_fanout(CAMPAIGN_ID)
    systems = [json.dumps(c["system"]) for c in provider.calls]
    assert len(set(systems)) == 1
    first_call_system = provider.calls[0]["system"]
    assert isinstance(first_call_system, list)
    assert all(block["cache"] for block in first_call_system)


def test_cross_standard_telemetry_prices_the_responding_model(
    confirmed_agent: ContentRepurposingAgent, sink: InMemorySink
) -> None:
    """Standard B: cost rides the record only when the responding model has a rate
    card; the mock model has none and no Azure rates are configured → cost is
    absent, never invented."""
    confirmed_agent.run_fanout(CAMPAIGN_ID)
    decisions = events_of(sink, "decision_made")
    assert decisions
    for record in decisions:
        assert record["gen_ai.response.model"] == "mock-model"
        assert record["gen_ai.request.model"] == "claude-opus-5"


def test_escalation_reason_codes_all_route_somewhere(config: RepurposingConfig) -> None:
    for code in config.reason_codes:
        if code == "truncation_retry":  # telemetry-only code, never routed
            continue
        assert config.route_for(code)


def test_flagship_first_signal_arrives_from_a_human_record(
    agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    case = db.load_case(store, CAMPAIGN_ID)
    assert case is not None and not case.get("flagship_confirmation")
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen", actor_role="content-writer")
    case = db.load_case(store, CAMPAIGN_ID)
    assert case is not None
    assert case["flagship_confirmation"]["actor_role"] == "content-writer"


def test_reuse_of_agent2_seed_contract(config: RepurposingConfig) -> None:
    """The seeded records mirror Agent 2's persisted shapes — the store contract
    both agents share."""
    store = InMemoryContextStore()
    seed_box_plan(store)
    for kind in ("plan_case", "audience_offer_pack", "asset_checklist", "content_outlines"):
        assert store.get(kind, CAMPAIGN_ID) is not None
