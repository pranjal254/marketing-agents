"""Bridge endpoint tests — mock provider, temp workdir, no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app

CLASSIFY_OK = json.dumps(
    {
        "action_class": "route_for_approval",
        "confidence": 0.9,
        "rationale": "explicit BU + vertical",
        "classification": {
            "campaign_type": "demand_gen",
            "priority": "high",
            "channel_mix": ["events", "email"],
            "segment_relevance": "type_3",
            "field_rationale": {"business_unit": "form field"},
        },
        "normalized_fields": {},
    }
)

COMPLETE_REQUEST = {
    "requester": "priya@x.com",
    "objective": "Pipeline for ERP offer",
    "business_unit": "Technology",
    "vertical": "manufacturing",
    "target_segment": "type_3",
    "offer_topic": "ERP modernization assessment",
    "channels": "events,email",
    "timeline_start": "2026-10-01",
    "timeline_end": "2026-11-15",
    "owner": "priya@x.com",
    "budget_flag": "yes",
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # mock provider abstains on {} — script the happy path through the bus default
    from shiftai_shared.llm import MockLLMProvider

    import c2c_bridge.app as app_mod

    def fake_build_provider(_: SharedSettings) -> MockLLMProvider:
        return MockLLMProvider(
            default=CLASSIFY_OK,
            script=[
                (
                    lambda u: "Draft one specific, targeted question" in u,
                    json.dumps({"questions": [{"field": "objective", "question": "Goal?"}]}),
                )
            ],
        )

    monkeypatch.setattr(app_mod, "build_provider", fake_build_provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _submit(client: TestClient, request: dict[str, Any] | None = None) -> dict[str, Any]:
    response = client.post(
        "/api/requests", json={"source": "form", "request": request or COMPLETE_REQUEST}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_health_and_meta(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["agent_id"] == "campaign_identification"
    assert health["kill_switch"] == "clear"
    meta = client.get("/api/meta").json()
    assert any(a["id"] == "route_for_approval" for a in meta["action_classes"])


def test_full_lifecycle_over_http(client: TestClient) -> None:
    outcome = _submit(client)
    assert outcome["status"] == "awaiting_approval"
    case_id = outcome["case_id"]

    cases = client.get("/api/cases").json()
    assert any(c["case_id"] == case_id for c in cases)

    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["approval_task"] is not None
    assert detail["summary"]["status"] == "awaiting_approval"

    # the brief document is downloadable
    doc_name = Path(str(outcome["doc_ref"])).name
    doc = client.get(f"/api/documents/{doc_name}")
    assert doc.status_code == 200 and doc.content[:2] == b"PK"

    decision = client.post(
        f"/api/cases/{case_id}/decision",
        json={"decision": "approved", "actor_id": "bu.lead@x.com"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"

    telemetry = client.get("/api/telemetry").json()
    events = [r["shiftai.event.type"] for r in telemetry if r["shiftai.case.id"] == case_id]
    for expected in (
        "case_intake",
        "decision_made",
        "action_taken",
        "human_gate",
        "case_resolved",
        "run_summary",
    ):
        assert expected in events
    assert all("bridge.seq" in r for r in telemetry)


def test_gap_flow_over_http(client: TestClient) -> None:
    outcome = _submit(client, {"requester": "a@x.com", "offer_topic": "AI thing"})
    assert outcome["status"] == "awaiting_input"
    case_id = outcome["case_id"]
    detail = client.get(f"/api/cases/{case_id}").json()
    assert detail["gap_request"] is not None
    assert detail["gap_request"]["case_id"] == case_id  # stored under the case id

    answers = {
        "objective": "Pipeline",
        "business_unit": "Technology",
        "vertical": "manufacturing",
        "target_segment": "type_4",
        "channels": "email",
        "timeline_start": "2026-10-01",
        "timeline_end": "2026-11-01",
        "owner": "a@x.com",
        "budget_flag": "yes",
    }
    resumed = client.post(
        f"/api/cases/{case_id}/answers", json={"answers": answers, "actor_id": "a@x.com"}
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "awaiting_approval"


def test_decision_errors(client: TestClient) -> None:
    missing = client.post(
        "/api/cases/nope/decision", json={"decision": "approved", "actor_id": "x"}
    )
    assert missing.status_code == 404
    outcome = _submit(client)
    twice = client.post(
        f"/api/cases/{outcome['case_id']}/decision",
        json={"decision": "approved", "actor_id": "bu.lead@x.com"},
    )
    assert twice.status_code == 200
    again = client.post(
        f"/api/cases/{outcome['case_id']}/decision",
        json={"decision": "approved", "actor_id": "bu.lead@x.com"},
    )
    assert again.status_code == 409  # already resolved — gate refuses


def test_kill_switch_toggle(client: TestClient) -> None:
    assert client.post(
        "/api/control/kill-switch", json={"paused": True, "reason": "demo"}
    ).json() == {"kill_switch": "paused"}
    outcome = _submit(client)
    assert outcome["status"] == "escalated"
    client.post("/api/control/kill-switch", json={"paused": False})
    assert client.get("/api/health").json()["kill_switch"] == "clear"


def test_document_traversal_blocked(client: TestClient) -> None:
    response = client.get("/api/documents/..%2F..%2Fsecrets.txt")
    assert response.status_code == 404


def test_reset_starts_fresh_session(client: TestClient) -> None:
    _submit(client)
    assert len(client.get("/api/cases").json()) == 1
    reset = client.post("/api/control/reset").json()
    assert reset["status"] == "reset"
    assert client.get("/api/cases").json() == []  # fresh store
    assert client.get("/api/telemetry").json() == []  # fresh bus
    outcome = _submit(client)  # agent fully functional after reset
    assert outcome["status"] == "awaiting_approval"


def test_intake_hold_revise_release_return_flow(client: TestClient) -> None:
    # scripted extraction/revision come from conftest-free inline provider fixture:
    # the default CLASSIFY_OK provider cannot parse extraction → request unchanged →
    # gap flow; that is fine for endpoint-level coverage.
    outcome = client.post(
        "/api/requests",
        json={
            "source": "adhoc",
            "hold_for_verification": True,
            "request": {"requester": "r@x.com", "free_text_context": "vague description"},
        },
    ).json()
    assert outcome["status"] == "awaiting_input"
    case_id = outcome["case_id"]

    answers = {
        "objective": "Pipeline",
        "business_unit": "Technology",
        "vertical": "manufacturing",
        "target_segment": "type_4",
        "offer_topic": "ERP assessments",
        "channels": "email",
        "timeline_start": "2026-10-01",
        "timeline_end": "2026-11-01",
        "owner": "r@x.com",
        "budget_flag": "yes",
    }
    held = client.post(
        f"/api/cases/{case_id}/answers", json={"answers": answers, "actor_id": "r@x.com"}
    ).json()
    assert held["status"] == "draft_review"

    revised = client.post(
        f"/api/cases/{case_id}/revise",
        json={"directive": "tighter objective", "actor_id": "r@x.com"},
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == "draft_review"

    released = client.post(f"/api/cases/{case_id}/release", json={"actor_id": "r@x.com"}).json()
    assert released["status"] == "awaiting_approval"

    returned = client.post(
        f"/api/cases/{case_id}/decision",
        json={"decision": "returned", "actor_id": "lead@x.com", "notes": "sharpen offer"},
    ).json()
    assert returned["status"] == "draft_review"
    summary = client.get(f"/api/cases/{case_id}").json()["summary"]
    assert summary["returned_note"] == "sharpen offer"

    client.post(f"/api/cases/{case_id}/release", json={"actor_id": "r@x.com"})
    approved = client.post(
        f"/api/cases/{case_id}/decision",
        json={"decision": "approved", "actor_id": "lead@x.com"},
    ).json()
    assert approved["status"] == "approved"
