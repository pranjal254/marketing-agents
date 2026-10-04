"""The gate covers three journey steps: compliance, grammar QA and sign-off.
A dashboard that reports all three as one number cannot answer the question
people actually ask, which is where the time before release goes.
"""

from __future__ import annotations

import pytest
from conftest import CAMPAIGN_ID, approve_all_asset_tasks, seed_manifest
from shiftai_shared.telemetry import InMemorySink

from c2c_quality_gate import persistence as db
from c2c_quality_gate.orchestration import QualityGateAgent

STAGE = "shiftai.stage.id"
ORDINAL = "shiftai.stage.ordinal"


@pytest.fixture()
def gated(agent: QualityGateAgent, workspace) -> QualityGateAgent:
    seed_manifest(agent.deps.store, workspace)
    agent.run_gate(CAMPAIGN_ID)
    return agent


def _stage_of(records: list[dict], since: int = 0) -> set[str]:
    return {str(r[STAGE]) for r in records[since:] if STAGE in r}


def test_the_gate_pass_itself_is_compliance(
    gated: QualityGateAgent, sink: InMemorySink
) -> None:
    assert sink.records, "the gate pass emitted nothing"
    assert _stage_of(sink.records) == {"compliance"}
    assert all(r[ORDINAL] == 7 for r in sink.records if STAGE in r)


def test_an_asset_review_decision_is_grammar_qa(
    gated: QualityGateAgent, sink: InMemorySink
) -> None:
    task = next(
        t for t in db.load_tasks(gated.deps.store, CAMPAIGN_ID) if t.scope == "asset"
    )
    before = len(sink.records)
    gated.record_review_outcome(
        CAMPAIGN_ID, task.task_id, decision="approved",
        actor_id="qa@x", actor_role="grammar-quality-reviewer",
    )
    emitted = sink.records[before:]
    assert emitted, "the review decision emitted nothing"
    assert _stage_of(emitted) == {"grammar_qa"}
    assert all(r[ORDINAL] == 8 for r in emitted if STAGE in r)


def test_the_package_signoff_is_its_own_step(
    gated: QualityGateAgent, sink: InMemorySink
) -> None:
    approve_all_asset_tasks(gated)
    package = next(
        t for t in db.load_tasks(gated.deps.store, CAMPAIGN_ID) if t.scope == "package"
    )
    before = len(sink.records)
    gated.record_review_outcome(
        CAMPAIGN_ID, package.task_id, decision="approved",
        actor_id="lead@x", actor_role="bu-campaign-lead",
    )
    emitted = sink.records[before:]
    assert emitted, "the sign-off emitted nothing"
    assert _stage_of(emitted) == {"signoff"}
    assert all(r[ORDINAL] == 9 for r in emitted if STAGE in r)
