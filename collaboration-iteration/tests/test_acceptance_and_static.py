"""Acceptance (one check per spec Implementation Task 1-10) + static guardrails."""

from __future__ import annotations

import re
from pathlib import Path

from conftest import (
    CAMPAIGN_ID,
    RecordingSignals,
    add_default_feedback,
    events_of,
)
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.telemetry import InMemorySink

from c2c_collaboration import persistence as db
from c2c_collaboration.orchestration import CollaborationAgent

ASSET = "linkedin_posts"
SRC = Path(__file__).resolve().parents[1] / "src" / "c2c_collaboration"


def _full_cycle(agent: CollaborationAgent) -> None:
    agent.on_draft_staged(CAMPAIGN_ID, ASSET)
    add_default_feedback(agent)
    outcome = agent.run_review_round(CAMPAIGN_ID, ASSET, actor_id="jen@x.com")
    agent.resolve_conflict(CAMPAIGN_ID, ASSET, outcome.conflicts[0].conflict_id,
                           decision="Keep it formal", actor_id="rishi@x.com")
    agent.confirm_content(CAMPAIGN_ID, ASSET, actor_id="jen@x.com",
                          actor_role="content-writer")


def test_steps_1_to_10_land_their_artifacts(
    agent: CollaborationAgent, store: InMemoryContextStore,
    sink: InMemorySink, signals: RecordingSignals,
) -> None:
    _full_cycle(agent)
    state = db.load_state(store, CAMPAIGN_ID, ASSET)
    assert state is not None
    # 1 assign reviewers + due dates; 2-3 collect/normalize/de-conflict;
    # 4 classify (textual/structural/out-of-scope); 5 tracked new version with
    # markers protected; 6 edit summary; 7 state machine + human-only confirm;
    # 8 confirmation signal; 9 sweep (unit-tested); 10 metrics.
    rounds = db.load_rounds(store, CAMPAIGN_ID, ASSET)
    assert rounds[0].edit_summary
    assert rounds[0].normalized and rounds[0].resolutions
    assert state.status == "content_confirmed" and state.confirmed_by == "jen@x.com"
    assert any(c[0] == "register_confirmed" for c in signals.calls)
    assert store.get(db.KIND_METRICS, f"{CAMPAIGN_ID}:{ASSET}") is not None
    assert events_of(sink, "human_gate")  # identity-stamped gates in the stream


def test_telemetry_identity_and_agent_type(
    agent: CollaborationAgent, sink: InMemorySink,
) -> None:
    _full_cycle(agent)
    records = sink.records
    assert records
    assert all(r["shiftai.agent.id"] == "collaboration_iteration" for r in records)
    assert all(r["shiftai.agent.type"] == "decision" for r in records)
    # Trace continuity with the campaign (Agent 2's trace).
    assert any(r["shiftai.trace.id"] == "trace_box_test" for r in records)


def test_escalation_reason_codes_all_route_somewhere(agent: CollaborationAgent) -> None:
    for code in agent.deps.config.reason_codes:
        assert agent.deps.config.route_for(code)


# ------------------------------------------------------------ static guardrails


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")}


def test_agent_never_confirms_or_resolves_on_its_own() -> None:
    """content_confirmed and conflict resolution are human-only: no module in the
    package invokes the gates; only definitions exist (the CLI carries a human's
    identity and the bridge does the same)."""
    for name, text in _sources().items():
        stripped = re.sub(r'""".*?"""', "", text, flags=re.DOTALL)
        if name == "cli.py":
            continue  # the CLI forwards a HUMAN's identity — allowed
        assert "self.confirm_content(" not in stripped, name
        assert ".confirm_content(" not in stripped or name == "orchestration.py", name
        if name == "orchestration.py":
            # Only the definition — no self-invocation of either gate.
            assert "self.confirm_content(" not in stripped
            assert "self.resolve_conflict(" not in stripped


def test_no_publish_or_send_surface_exists() -> None:
    forbidden = re.compile(
        r"^\s*(import|from)\s+(requests|httpx|urllib3|aiohttp|smtplib|slack_sdk|tweepy|"
        r"linkedin|salesforce|simple_salesforce|pardot)\b",
        re.MULTILINE,
    )
    for name, text in _sources().items():
        assert not forbidden.search(text), f"forbidden connector import in {name}"


def test_no_destructive_file_operations() -> None:
    forbidden = re.compile(
        r"\b(os\.remove|os\.unlink|os\.rename|shutil\.|\.unlink\(|\.rmdir\(|"
        r"write_text\(|write_bytes\()"
    )
    for name, text in _sources().items():
        assert not forbidden.search(text), f"destructive file operation in {name}"


def test_verbatim_system_prompt_is_versioned_and_untouched() -> None:
    prompt = (
        Path(__file__).resolve().parents[1] / "prompts"
        / "collaboration-iteration.system.v1.0.0.md"
    ).read_text(encoding="utf-8")
    assert "You are the Content Collaboration & Iteration Agent" in prompt
    assert "Never adjudicate conflicting feedback" in prompt
    assert "content_confirmed is set only by a human reviewer" in prompt
    assert "Never remove or weaken a claim-to-source marker" in prompt


def test_agent4_kinds_are_in_the_governance_catalog() -> None:
    migration = (
        Path(__file__).resolve().parents[2] / "shared" / "src" / "shiftai_shared"
        / "context_store" / "migrations" / "0003_capability_c2c_agent4.sql"
    ).read_text(encoding="utf-8")
    for kind in (db.KIND_REVIEW_STATE, db.KIND_FEEDBACK, db.KIND_ROUND,
                 db.KIND_CONFLICT, db.KIND_METRICS):
        assert f"('{kind}'" in migration, f"kind {kind!r} missing from catalog migration"
