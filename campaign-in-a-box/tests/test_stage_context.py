"""This agent spans three journey steps, so its telemetry has to say which one
each record belongs to. Getting that wrong is invisible in the product and only
shows up as a dashboard that blames the wrong step for the cost.
"""

from __future__ import annotations

from typing import Any

from shiftai_shared.telemetry import InMemorySink

from c2c_campaign_box.orchestration import CampaignBoxOrchestrator
from tests.conftest import CAMPAIGN_ID, run_plan
from tests.test_orchestration_e2e import (
    _confirm_both,
    _register_all,
)

STAGE = "shiftai.stage.id"
ORDINAL = "shiftai.stage.ordinal"


def _stages(sink: InMemorySink) -> set[str]:
    return {str(r[STAGE]) for r in sink.records if STAGE in r}


def _action(records: list[dict[str, Any]], action: str) -> dict[str, Any]:
    return next(r for r in records if r.get("shiftai.decision.action_class") == action)


def test_every_record_carries_process_and_stage(
    orchestrator: CampaignBoxOrchestrator, sink: InMemorySink
) -> None:
    run_plan(orchestrator)
    assert sink.records, "the planning pass emitted nothing"
    for record in sink.records:
        assert record["shiftai.process.name"] == "content-to-campaign"
        assert record["shiftai.process.version"]
        assert record[STAGE], f"{record['shiftai.event.type']} carries no stage"
        assert isinstance(record[ORDINAL], int)


def test_the_pack_is_step_two_and_the_checklist_is_step_three(
    orchestrator: CampaignBoxOrchestrator, sink: InMemorySink
) -> None:
    """Both happen inside one planning run, so a run-level stage is not enough:
    the audience and offer work is a step the business tracks separately."""
    run_plan(orchestrator)
    pack = _action(sink.records, "audience_offer_pack")
    checklist = _action(sink.records, "asset_checklist")

    assert pack[STAGE] == "audience_offer"
    assert pack[ORDINAL] == 2
    assert checklist[STAGE] == "asset_plan"
    assert checklist[ORDINAL] == 3


def test_packaging_records_are_step_six_not_the_planning_default(
    orchestrator: CampaignBoxOrchestrator, sink: InMemorySink
) -> None:
    run_plan(orchestrator)
    _confirm_both(orchestrator)
    _register_all(orchestrator)
    before = len(sink.records)

    packaged = orchestrator.run_packaging(CAMPAIGN_ID)
    assert packaged.status == "packaged_pending_compliance"

    packaging_records = sink.records[before:]
    assert packaging_records, "packaging emitted nothing"
    assert {str(r[STAGE]) for r in packaging_records} == {"packaging"}
    assert all(r[ORDINAL] == 6 for r in packaging_records)


def test_a_full_run_reports_the_steps_this_agent_actually_covers(
    orchestrator: CampaignBoxOrchestrator, sink: InMemorySink
) -> None:
    run_plan(orchestrator)
    _confirm_both(orchestrator)
    _register_all(orchestrator)
    orchestrator.run_packaging(CAMPAIGN_ID)
    assert _stages(sink) == {"audience_offer", "asset_plan", "packaging"}
