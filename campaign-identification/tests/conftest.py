"""Test harness: fully mocked connectors, in-memory stores, scripted LLM.

No live API calls, no real credentials, anywhere (session rule + spec governance).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from shiftai_shared.business_capability import DecisionAgentConfig, load_decision_config
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import MockLLMProvider
from shiftai_shared.resilience import InMemoryIdempotencyStore
from shiftai_shared.telemetry import InMemorySink

from campaign_identification.orchestration import AgentDeps, CampaignIdentificationAgent

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "campaign_identification.json"

CLASSIFY_OK = json.dumps(
    {
        "action_class": "route_for_approval",
        "confidence": 0.92,
        "rationale": "BU and vertical explicit on the request; plan-linked demand gen",
        "classification": {
            "campaign_type": "demand_gen",
            "priority": "high",
            "channel_mix": ["events", "email", "linkedin"],
            "segment_relevance": "type_3 manufacturing accounts; events emphasized",
            "field_rationale": {
                "business_unit": "intake form field 'business_unit'",
                "vertical": "intake form field 'vertical'",
            },
        },
        "normalized_fields": {},
    }
)

GAP_QUESTIONS = json.dumps(
    {
        "questions": [
            {
                "field": "objective",
                "question": "What business outcome should this campaign drive?",
            }
        ]
    }
)


class InMemoryWorkspace:
    def __init__(self) -> None:
        self.documents: dict[str, bytes] = {}
        self.uploads = 0

    def upload_document(self, filename: str, content: bytes) -> str:
        self.uploads += 1
        self.documents[filename] = content
        return f"drive-item-{filename}"


def scripted_provider() -> MockLLMProvider:
    return MockLLMProvider(
        default=CLASSIFY_OK,
        script=[(lambda u: "Draft one specific, targeted question" in u, GAP_QUESTIONS)],
        model_name="mock-sonnet",
    )


@pytest.fixture()
def config() -> DecisionAgentConfig:
    return load_decision_config(CONFIG_PATH)


@pytest.fixture()
def harness(config: DecisionAgentConfig) -> dict[str, Any]:
    provider = scripted_provider()
    sink = InMemorySink()
    workspace = InMemoryWorkspace()
    deps = AgentDeps(
        provider=provider,
        store=InMemoryContextStore(),
        workspace=workspace,
        sink=sink,
        kill_switch=KillSwitch(),
        rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=1000),
        idempotency=InMemoryIdempotencyStore(),
        config=config,
        # _env_file=None: a developer's local .env must not leak into unit tests
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock", SHIFTAI_ENVIRONMENT="dev"),
    )
    agent = CampaignIdentificationAgent(deps)
    return {
        "agent": agent,
        "deps": deps,
        "sink": sink,
        "provider": provider,
        "workspace": workspace,
    }


@pytest.fixture()
def complete_raw() -> dict[str, Any]:
    return json.loads(
        (AGENT_ROOT / "samples" / "sample-request-complete.json").read_text(encoding="utf-8")
    )


@pytest.fixture()
def incomplete_raw() -> dict[str, Any]:
    return json.loads(
        (AGENT_ROOT / "samples" / "sample-request-incomplete.json").read_text(encoding="utf-8")
    )


def events(sink: InMemorySink, case_id: str | None = None) -> list[str]:
    return [
        str(r["shiftai.event.type"])
        for r in sink.records
        if case_id is None or r["shiftai.case.id"] == case_id
    ]


def records_of(
    sink: InMemorySink, event_type: str, case_id: str | None = None
) -> list[dict[str, Any]]:
    return [
        r
        for r in sink.records
        if r["shiftai.event.type"] == event_type
        and (case_id is None or r["shiftai.case.id"] == case_id)
    ]
