"""Agent 2 bridge endpoints — approved brief (Agent 1) → planning → confirmation →
asset stand-ins → packaging, all over HTTP with a scripted mock provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app
from tests.test_bridge import CLASSIFY_OK, COMPLETE_REQUEST

PACK_JSON = json.dumps(
    {
        "segment_applicability": {"type_3": "per brief"},
        "personas": [
            {"persona_id": "mandate_owner", "title": "Mandate Owner",
             "role_pains": "fragmentation", "rationale": "brief:objective"}
        ],
        "value_proposition": "ShiftAI-led modernization",
        "differentiators": ["single partner"],
        "proof_points": [
            {"claim": "Brief topic is ERP modernization",
             "source_ref": "brief:offer_topic", "status": "verified"}
        ],
        "ctas": {"awareness": "Read the brief"},
        "messaging_angles": [
            {"persona_id": "mandate_owner", "angle": "Modernize without disruption",
             "grounding": "brief:objective"}
        ],
        "channel_emphasis": {"events": "proven for type_3 (brief:target_segment)"},
        "gaps": [],
        "confidence": 0.9,
    }
)

REUSE_JSON = json.dumps(
    {
        "items": [
            {
                "asset_id": "flagship_blog",
                "decision": "create",
                "rationale": "no fitting candidate",
                "reuse_ref": None,
                "outline": {
                    "asset_id": "flagship_blog",
                    "asset_type": "flagship_blog",
                    "title": "ERP modernization",
                    "sections": [
                        {"heading": "Problem", "notes": "hook",
                         "planned_claims": ["brief:offer_topic"]}
                    ],
                    "seeded_from_angles": ["mandate_owner"],
                },
            }
        ],
        "confidence": 0.8,
    }
)

REQUIRED = ["flagship_blog", "email_touchpoints", "linkedin_posts", "faq_service_page",
            "external_one_pager", "call_scripts"]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from shiftai_shared.llm import MockLLMProvider

    import c2c_bridge.app as app_mod

    def fake_build_provider(_: SharedSettings) -> MockLLMProvider:
        return MockLLMProvider(
            default=CLASSIFY_OK,
            script=[
                (lambda u: "audience & offer pack" in u, PACK_JSON),
                (lambda u: "reuse / adapt / create" in u, REUSE_JSON),
            ],
        )

    monkeypatch.setattr(app_mod, "build_provider", fake_build_provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _approved_campaign(client: TestClient) -> str:
    outcome = client.post(
        "/api/requests", json={"source": "form", "request": COMPLETE_REQUEST}
    ).json()
    case_id = outcome["case_id"]
    approved = client.post(
        f"/api/cases/{case_id}/decision",
        json={"decision": "approved", "actor_id": "bu.lead@x.com"},
    ).json()
    campaign_id = str(approved["brief"]["campaign_id"])
    return campaign_id


def _plan(client: TestClient, campaign_id: str) -> dict[str, Any]:
    response = client.post(
        f"/api/box/campaigns/{campaign_id}/plan", json={"actor_id": "studio@x.com"}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_meta_exposes_box_agent(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["box"]["agent_id"] == "campaign_in_a_box"
    assert meta["box"]["intel_mode"] == "intel_library_only"  # no key in tests
    assert any(c["asset_type"] == "flagship_blog" for c in meta["box"]["composition"])


def test_planning_requires_approved_brief(client: TestClient) -> None:
    outcome = _plan(client, "cmp_never_approved")
    assert outcome["status"] == "failed"
    assert outcome["escalation_reasons"] == ["brief_not_approved"]


def test_full_box_lifecycle_over_http(client: TestClient) -> None:
    campaign_id = _approved_campaign(client)

    planned = _plan(client, campaign_id)
    assert planned["status"] == "awaiting_confirmation"
    assert planned["pack"]["intel_mode"] == "intel_library_only"
    # Reuses Agent 1's trace so the journey reconstructs across both agents.
    telemetry = client.get("/api/telemetry").json()
    box_traces = {
        r["shiftai.trace.id"] for r in telemetry
        if r["shiftai.agent.id"] == "campaign_in_a_box"
    }
    agent1_traces = {
        r["shiftai.trace.id"] for r in telemetry
        if r["shiftai.agent.id"] == "campaign_identification"
    }
    assert box_traces <= agent1_traces

    summaries = client.get("/api/box/campaigns").json()
    assert [s["campaign_id"] for s in summaries] == [campaign_id]

    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    assert detail["pack"] is not None and detail["checklist"] is not None
    assert detail["plan"]["feasible"] is True

    for kind in ("pack", "plan"):
        confirmed = client.post(
            f"/api/box/campaigns/{campaign_id}/confirm",
            json={"kind": kind, "actor_id": "marketing.lead@x.com"},
        )
        assert confirmed.status_code == 200, confirmed.text
    assert (
        client.get(f"/api/box/campaigns/{campaign_id}").json()["summary"]["status"]
        == "in_production"
    )

    # Packaging blocks while assets are missing (actionable report).
    blocked = client.post(f"/api/box/campaigns/{campaign_id}/package").json()
    assert blocked["status"] == "packaging_blocked"
    assert blocked["report"]["diff"]["missing"]

    for asset_id in REQUIRED:
        confirmed_asset = client.post(
            f"/api/box/campaigns/{campaign_id}/assets/{asset_id}/confirm",
            json={"actor_id": "reviewer@x.com", "claim_refs": ["brief:offer_topic"]},
        )
        assert confirmed_asset.status_code == 200, confirmed_asset.text

    packaged = client.post(f"/api/box/campaigns/{campaign_id}/package").json()
    assert packaged["status"] == "packaged_pending_compliance"
    manifest = packaged["manifest"]
    assert {a["asset_id"] for a in manifest["assets"]} == set(REQUIRED)

    # Snapshot download through the workspace-scoped endpoint.
    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    folder = detail["case"]["folder"]
    snapshot_name = manifest["assets"][0]["canonical_name"]
    doc = client.get(f"/api/box/documents?path={folder}/final/{snapshot_name}")
    assert doc.status_code == 200 and doc.content[:2] == b"PK"

    # Rework re-open only affects the named asset; re-package bumps the manifest.
    reopened = client.post(
        f"/api/box/campaigns/{campaign_id}/reopen",
        json={"asset_ids": ["flagship_blog"], "actor_id": "gate@x.com"},
    ).json()
    assert reopened["status"] == "in_production"
    client.post(
        f"/api/box/campaigns/{campaign_id}/assets/flagship_blog/confirm",
        json={"actor_id": "reviewer@x.com", "text": "reworked flagship"},
    )
    repackaged = client.post(f"/api/box/campaigns/{campaign_id}/package").json()
    assert repackaged["manifest"]["version"] == 2


def test_kill_switch_pauses_box_agent_too(client: TestClient) -> None:
    campaign_id = _approved_campaign(client)
    client.post("/api/control/kill-switch", json={"paused": True, "reason": "drill"})
    outcome = _plan(client, campaign_id)
    assert outcome["status"] == "escalated"
    assert "control_pause" in outcome["escalation_reasons"]
    client.post("/api/control/kill-switch", json={"paused": False})
    assert _plan(client, campaign_id)["status"] == "awaiting_confirmation"


def test_box_document_traversal_blocked(client: TestClient) -> None:
    response = client.get("/api/box/documents?path=..%2F..%2F.env")
    assert response.status_code == 404
