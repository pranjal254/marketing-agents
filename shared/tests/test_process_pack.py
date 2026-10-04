"""The journey pack is versioned capability content: validate its shape, and
guard the one failure mode that is silent — an agent id that no longer matches
the pack, which would strip stage context from every record it emits.
"""

from __future__ import annotations

import json
from pathlib import Path

from shiftai_shared.process import (
    PROCESS_NAME,
    PROCESS_PACK,
    process_context,
    stages,
)
from shiftai_shared.telemetry import InMemorySink, StsEmitter

AGENTS_ROOT = Path(__file__).resolve().parents[2]


def _configured_agent_ids() -> set[str]:
    """Every agentId the shipped Business Capability configs declare."""
    ids: set[str] = set()
    for config in AGENTS_ROOT.glob("*/config/*.json"):
        data = json.loads(config.read_text(encoding="utf-8"))
        agent_id = data.get("agentId")
        if agent_id:
            ids.add(str(agent_id))
    return ids


def test_stage_ordinals_are_contiguous_and_unique() -> None:
    ordinals = [s.ordinal for s in stages()]
    assert ordinals == sorted(ordinals)
    assert ordinals == list(range(1, len(ordinals) + 1))
    assert len({s.stage_id for s in stages()}) == len(ordinals)


def test_every_configured_agent_has_a_default_stage() -> None:
    """A renamed agentId silently drops stage context; fail here instead."""
    configured = _configured_agent_ids()
    assert configured, "no agent configs found — the glob is wrong"
    missing = sorted(a for a in configured if a not in PROCESS_PACK.agent_default_stages)
    assert not missing, f"agents with no stage in the journey pack: {missing}"


def test_pack_maps_no_unknown_agents() -> None:
    """The reverse: a stale mapping entry means a rename was half-applied."""
    stale = sorted(set(PROCESS_PACK.agent_default_stages) - _configured_agent_ids())
    assert not stale, f"journey pack maps agents that no longer exist: {stale}"


def test_emitter_stamps_process_and_stage() -> None:
    sink = InMemorySink()
    emitter = StsEmitter(
        sink,
        tenant_id="t1",
        agent_id="quality_gate_approval",
        agent_type="decision",
        config_version="0.1.0",
        environment="dev",
        risk_tier="high",
        data_classification="confidential",
        process=process_context("quality_gate_approval"),
    )
    record = emitter.emit("case_intake", case_id="c1", trace_id="tr1")
    assert record["shiftai.process.name"] == PROCESS_NAME
    assert record["shiftai.process.version"] == PROCESS_PACK.version
    assert record["shiftai.stage.id"] == "compliance"
    assert record["shiftai.stage.ordinal"] == 7


def test_per_call_stage_override_wins_and_rederives_the_ordinal() -> None:
    sink = InMemorySink()
    emitter = StsEmitter(
        sink,
        tenant_id="t1",
        agent_id="quality_gate_approval",
        agent_type="decision",
        config_version="0.1.0",
        environment="dev",
        risk_tier="high",
        data_classification="confidential",
        process=process_context("quality_gate_approval"),
    )
    record = emitter.emit(
        "case_intake", case_id="c1", trace_id="tr1", **{"shiftai.stage.id": "signoff"}
    )
    assert record["shiftai.stage.id"] == "signoff"
    assert record["shiftai.stage.ordinal"] == 9
