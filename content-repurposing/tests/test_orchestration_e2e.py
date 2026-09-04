"""End-to-end state machine tests: flagship → human confirm → fan-out → rework,
all with mocked providers and in-memory stores."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from c2c_campaign_box.workspace import LocalCampaignWorkspace, WorkspaceWriteError
from conftest import (
    CAMPAIGN_ID,
    DERIVATIVE_JSON,
    FLAGSHIP_JSON,
    build_agent,
    events_of,
    seed_box_plan,
)
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch
from shiftai_shared.llm import MockLLMProvider
from shiftai_shared.telemetry import InMemorySink

from c2c_content_repurposing import persistence as db
from c2c_content_repurposing.agent_config import RepurposingConfig
from c2c_content_repurposing.orchestration import (
    ContentRepurposingAgent,
    RepurposeGateError,
    SequencingViolationError,
)


def test_flagship_happy_path_stages_versioned_draft(
    agent: ContentRepurposingAgent,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    store: InMemoryContextStore,
) -> None:
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "flagship_staged"
    assert outcome.draft is not None and outcome.draft.version == 1
    assert outcome.draft.status == "staged"
    # The refused outline section rides along as an explicit gap note.
    assert any("made-up-competitor-stat" in g.needed for g in outcome.gap_notes)
    # Draft + claim-map sidecar landed in the campaign workspace drafts folder.
    names = {f.name for f in workspace.list_files("2026-Q4-erp-modernization/drafts")}
    assert outcome.draft.filename in names
    assert outcome.draft.filename.removesuffix(".docx") + ".claims.json" in names
    # Registered in the Context Store with markers.
    stored = db.latest_draft(store, CAMPAIGN_ID, "flagship_blog")
    assert stored is not None and stored.claim_markers[0].source_ref == "sig:1"
    # Telemetry: L3 decision carries prompt/template/model identity + cost scope.
    decisions = events_of(sink, "decision_made")
    assert decisions and decisions[0]["shiftai.prompt.template.id"] == (
        "content-repurposing-flagship"
    )
    assert decisions[0]["gen_ai.response.model"] == "mock-model"
    assert events_of(sink, "action_taken")
    assert events_of(sink, "run_summary")
    # Trace continuity with Agent 2.
    assert decisions[0]["shiftai.trace.id"] == "trace_agent2_test"


def test_flagship_twice_is_a_gate_error(agent: ContentRepurposingAgent) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    with pytest.raises(RepurposeGateError):
        agent.draft_flagship(CAMPAIGN_ID)


def test_flagship_selfcheck_regenerates_then_passes(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    bad = json.loads(FLAGSHIP_JSON)
    bad["sections"][1]["paragraphs"] = ["Shift AI helps here."]  # brand lint error
    provider = MockLLMProvider(default=json.dumps(bad))
    original_complete = provider.complete

    def complete(**kwargs: object) -> object:
        # First call answers with the bad draft, every later call with the clean one.
        response = original_complete(**kwargs)  # type: ignore[arg-type]
        provider.default = FLAGSHIP_JSON
        return response

    provider.complete = complete  # type: ignore[method-assign]
    agent = build_agent(provider, store, workspace, sink, config, settings)
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "flagship_staged"
    assert outcome.draft is not None and outcome.draft.self_check.attempts == 2


def test_flagship_persistent_selfcheck_failure_withholds_and_escalates(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    bad = json.loads(FLAGSHIP_JSON)
    bad["sections"][1]["paragraphs"] = ["Shift AI helps here."]
    provider = MockLLMProvider(default=json.dumps(bad))
    agent = build_agent(provider, store, workspace, sink, config, settings)
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "escalated"
    assert outcome.draft is not None and outcome.draft.status == "withheld"
    # Never staged: nothing landed in the workspace.
    assert workspace.list_files("2026-Q4-erp-modernization/drafts") == []
    codes = [e.get("shiftai.learn.reason_code") for e in events_of(sink, "case_escalated")]
    assert "selfcheck_failed" in codes
    # Regenerated exactly maxRegenerations extra times, then withheld.
    assert outcome.draft.self_check.attempts == config.max_regenerations + 1


def test_flagship_all_sections_unverified_escalates_without_llm(
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    store = InMemoryContextStore()
    seed_box_plan(store, flagship_claims=["totally-unverified-ref"])
    provider = MockLLMProvider(default="{}")
    agent = build_agent(provider, store, workspace, sink, config, settings)
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "escalated"
    assert outcome.escalation_reasons == ["unsourced_claim"]
    assert provider.calls == []  # refused BEFORE any model call


def test_flagship_unparsable_output_escalates_tool_failure(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    provider = MockLLMProvider(default="this is not json")
    agent = build_agent(provider, store, workspace, sink, config, settings)
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "escalated"
    assert "tool_failure" in outcome.escalation_reasons


# ------------------------------------------------------------------ confirm gate


def test_confirm_flagship_records_identity_stamped_gate(
    agent: ContentRepurposingAgent, sink: InMemorySink, store: InMemoryContextStore
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    outcome = agent.confirm_flagship(
        CAMPAIGN_ID, actor_id="jen.cook@levelshift.com", actor_role="content-writer"
    )
    assert outcome.status == "flagship_confirmed"
    gates = events_of(sink, "human_gate")
    assert gates and gates[-1]["shiftai.hitl.decision"] == "approved"
    case = db.load_case(store, CAMPAIGN_ID)
    assert case is not None
    assert case["flagship_confirmation"]["actor_id"] == "jen.cook@levelshift.com"


def test_confirm_requires_staged_flagship(agent: ContentRepurposingAgent) -> None:
    with pytest.raises(RepurposeGateError):
        agent.confirm_flagship(CAMPAIGN_ID, actor_id="someone")


# ---------------------------------------------------------------------- fan-out


def test_fanout_before_confirmation_is_a_sequencing_violation(
    agent: ContentRepurposingAgent, sink: InMemorySink
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    with pytest.raises(SequencingViolationError):
        agent.run_fanout(CAMPAIGN_ID)
    codes = [e.get("shiftai.learn.reason_code") for e in events_of(sink, "case_escalated")]
    assert "sequencing_violation" in codes


def test_fanout_stages_derivatives_with_lineage(
    agent: ContentRepurposingAgent,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    store: InMemoryContextStore,
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen.cook@levelshift.com")
    outcome = agent.run_fanout(CAMPAIGN_ID)
    assert outcome.status == "derivatives_staged"
    staged_ids = {d.asset_id for d in outcome.staged}
    # create/adapt items with recipes are drafted; reuse (battle_card) is skipped.
    assert staged_ids == {"linkedin_posts", "faq_service_page"}
    assert "battle_card" in outcome.skipped
    # Volume caps: linkedin volume=2 → 2 variants kept of the 3 offered.
    linkedin = next(d for d in outcome.staged if d.asset_id == "linkedin_posts")
    assert len(linkedin.sections) == 2
    # 100% claim-lineage coverage — every derivative cites inventory items.
    assert all(d.claim_lineage == ["cl-1"] for d in outcome.staged)
    # Inventory was verified from verbatim quotes (the paraphrased item dropped).
    assert outcome.inventory is not None
    assert outcome.inventory.method == "llm_verified"
    assert outcome.inventory.dropped_unverified == 1
    # Drafts + claim maps + inventory landed in the workspace.
    names = {f.name for f in workspace.list_files("2026-Q4-erp-modernization/drafts")}
    assert any(n.startswith("erp-modernization-linkedin-posts") for n in names)
    assert any("claim-inventory" in n for n in names)
    summary = events_of(sink, "run_summary")[-1]
    assert summary["shiftai.fanout.staged"] == 2
    assert summary["shiftai.fanout.claim_lineage_coverage"] == 1.0


def test_fanout_inventory_falls_back_deterministically(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    provider = MockLLMProvider(
        script=[
            (lambda u: "Draft the flagship asset" in u, FLAGSHIP_JSON),
            (lambda u: u.startswith("Extract the confirmed flagship's claim inventory"),
             "not json at all"),
            (lambda u: "derivative from the claim inventory" in u, DERIVATIVE_JSON),
        ],
        default="{}",
    )
    agent = build_agent(provider, store, workspace, sink, config, settings)
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    outcome = agent.run_fanout(CAMPAIGN_ID)
    assert outcome.inventory is not None
    assert outcome.inventory.method == "deterministic_fallback"
    # Fallback inventory comes from the flagship's own verified claim map.
    assert outcome.inventory.items[0].source_ref == "sig:1"


def test_fanout_withholds_failing_assets_and_stages_the_rest(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    bad_derivative = json.loads(DERIVATIVE_JSON)
    bad_derivative["variants"] = [
        {"label": "v", "paragraphs": ["We grew 87% in a year, guaranteed."]}
    ]

    def is_faq(user: str) -> bool:
        return "FAQ / AEO" in user

    provider = MockLLMProvider(
        script=[
            (lambda u: "Draft the flagship asset" in u, FLAGSHIP_JSON),
            (lambda u: u.startswith("Extract the confirmed flagship's claim inventory"), "nope"),
            (is_faq, json.dumps(bad_derivative)),  # unsourced 87% → withheld
            (lambda u: "derivative from the claim inventory" in u, DERIVATIVE_JSON),
        ],
        default="{}",
    )
    agent = build_agent(provider, store, workspace, sink, config, settings)
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    outcome = agent.run_fanout(CAMPAIGN_ID)
    assert outcome.withheld == ["faq_service_page"]
    assert {d.asset_id for d in outcome.staged} == {"linkedin_posts"}
    codes = [e.get("shiftai.learn.reason_code") for e in events_of(sink, "case_escalated")]
    assert "selfcheck_failed" in codes
    # The withheld asset never reached the workspace.
    names = {f.name for f in workspace.list_files("2026-Q4-erp-modernization/drafts")}
    assert not any("faq" in n for n in names)
    # A gap note explains what is needed.
    assert any(g.asset_id == "faq_service_page" for g in outcome.gap_notes)


def test_fanout_rerun_skips_already_staged(
    agent: ContentRepurposingAgent, provider: MockLLMProvider
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    agent.run_fanout(CAMPAIGN_ID)
    calls_before = len(provider.calls)
    second = agent.run_fanout(CAMPAIGN_ID)
    assert second.staged == []  # nothing regenerated
    assert len(provider.calls) == calls_before  # no new LLM calls


def test_fanout_kill_switch_pauses_before_side_effects(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
    provider: MockLLMProvider,
) -> None:
    kill = KillSwitch()
    agent = build_agent(provider, store, workspace, sink, config, settings, kill_switch=kill)
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    kill.pause(config.agent_id, "governance pause")
    outcome = agent.run_fanout(CAMPAIGN_ID)
    assert outcome.escalation_reasons == ["control_pause"]
    assert outcome.staged == []


# ---------------------------------------------------------------------- rework


def test_rework_flagship_before_confirm_creates_new_version(
    agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    outcome = agent.apply_rework(
        CAMPAIGN_ID, "flagship_blog",
        instruction="Tighten the opening section", actor_id="jen",
    )
    assert outcome.draft is not None
    assert outcome.draft.version == 2
    assert outcome.draft.rework_of_version == 1
    # v1 is still in the store — additive, never overwritten.
    drafts = db.load_drafts(store, CAMPAIGN_ID)
    assert {d.version for d in drafts if d.asset_id == "flagship_blog"} == {1, 2}


def test_rework_flagship_after_confirm_is_refused(agent: ContentRepurposingAgent) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    with pytest.raises(RepurposeGateError, match="re-open"):
        agent.apply_rework(CAMPAIGN_ID, "flagship_blog",
                           instruction="change it", actor_id="jen")


def test_rework_derivative_regenerates_only_that_asset(
    agent: ContentRepurposingAgent, store: InMemoryContextStore
) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    agent.run_fanout(CAMPAIGN_ID)
    outcome = agent.apply_rework(
        CAMPAIGN_ID, "linkedin_posts",
        instruction="Make variant 1 shorter", actor_id="rishi",
    )
    assert outcome.draft is not None and outcome.draft.version == 2
    faq_versions = {
        d.version for d in db.load_drafts(store, CAMPAIGN_ID) if d.asset_id == "faq_service_page"
    }
    assert faq_versions == {1}  # untouched


def test_rework_requires_instruction(agent: ContentRepurposingAgent) -> None:
    agent.draft_flagship(CAMPAIGN_ID)
    with pytest.raises(RepurposeGateError):
        agent.apply_rework(CAMPAIGN_ID, "flagship_blog", instruction="  ", actor_id="jen")


# ------------------------------------------------------------------ workspace


def test_drafts_are_never_overwritten(tmp_path: Path) -> None:
    ws = LocalCampaignWorkspace(str(tmp_path))
    ws.upload("f", "a.docx", b"one")
    with pytest.raises(WorkspaceWriteError, match="overwrite"):
        ws.upload("f", "a.docx", b"two")


def test_telemetry_records_validate_against_sts_schema(
    agent: ContentRepurposingAgent, sink: InMemorySink
) -> None:
    """Every record the agent emits is validated at emit time by StsEmitter; this
    asserts the stream is non-trivial and carries the agent identity."""
    agent.draft_flagship(CAMPAIGN_ID)
    agent.confirm_flagship(CAMPAIGN_ID, actor_id="jen")
    agent.run_fanout(CAMPAIGN_ID)
    records = sink.records
    assert len(records) >= 8
    assert all(r["shiftai.agent.id"] == "content_repurposing" for r in records)
    assert all(r["shiftai.agent.type"] == "decision" for r in records)


def test_flagship_without_any_marker_stages_but_escalates(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> None:
    """Marker-free prose is legal (nothing invented) but must be VISIBLE: the
    fan-out would inherit an empty claim inventory (observed live, 2026-09-03)."""
    unmarked = json.loads(FLAGSHIP_JSON)
    unmarked["sections"] = [
        {"heading": "Prose only", "paragraphs": ["Calm positioning without claims."]}
    ]
    unmarked["claims_used"] = []
    provider = MockLLMProvider(default=json.dumps(unmarked))
    agent = build_agent(provider, store, workspace, sink, config, settings)
    outcome = agent.draft_flagship(CAMPAIGN_ID)
    assert outcome.status == "flagship_staged"  # staged — humans decide
    assert "unsourced_claim" in outcome.escalation_reasons
    codes = [e.get("shiftai.learn.reason_code") for e in events_of(sink, "case_escalated")]
    assert "unsourced_claim" in codes
