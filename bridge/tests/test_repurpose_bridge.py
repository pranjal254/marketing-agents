"""Agent 3 bridge endpoints — plan (Agent 2) → flagship draft → human confirm →
fan-out → per-asset confirm registers REAL drafts → packaging, all over HTTP."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app
from tests.test_box_bridge import PACK_JSON, REUSE_JSON
from tests.test_bridge import CLASSIFY_OK, COMPLETE_REQUEST

CLAIM = "The brief targets ERP modernization for manufacturers"
FLAGSHIP_PARA = f"{CLAIM} [c-1], and LevelShift carries it through one accountable plan."

FLAGSHIP_JSON = json.dumps(
    {
        "title": "ERP modernization without disruption",
        "sections": [{"heading": "Problem", "paragraphs": [FLAGSHIP_PARA]}],
        "claims_used": [
            {"marker": "c-1", "claim": CLAIM, "source_ref": "brief:offer_topic"}
        ],
        "gap_notes": [],
        "confidence": 0.9,
    }
)

INVENTORY_JSON = json.dumps(
    {
        "items": [
            {"claim_id": "raw-1", "kind": "claim", "text": CLAIM,
             "quote": CLAIM, "source_ref": "brief:offer_topic"}
        ],
        "confidence": 0.9,
    }
)

DERIVATIVE_JSON = json.dumps(
    {
        "title": "Channel derivative",
        "variants": [
            {"label": "Variant 1",
             "paragraphs": ["LevelShift keeps ERP modernization on one plan."]}
        ],
        "claims_used": ["cl-1"],
        "gap_notes": [],
        "confidence": 0.85,
    }
)


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
                (lambda u: "Draft the flagship asset" in u, FLAGSHIP_JSON),
                (lambda u: u.startswith("Extract the confirmed flagship's claim inventory"),
                 INVENTORY_JSON),
                (lambda u: "derivative from the claim inventory" in u, DERIVATIVE_JSON),
            ],
        )

    monkeypatch.setattr(app_mod, "build_provider", fake_build_provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _campaign_in_production(client: TestClient) -> str:
    outcome = client.post(
        "/api/requests", json={"source": "form", "request": COMPLETE_REQUEST}
    ).json()
    approved = client.post(
        f"/api/cases/{outcome['case_id']}/decision",
        json={"decision": "approved", "actor_id": "bu.lead@x.com"},
    ).json()
    campaign_id = str(approved["brief"]["campaign_id"])
    assert client.post(
        f"/api/box/campaigns/{campaign_id}/plan", json={"actor_id": "studio@x.com"}
    ).status_code == 200
    for kind in ("pack", "plan"):
        assert client.post(
            f"/api/box/campaigns/{campaign_id}/confirm",
            json={"kind": kind, "actor_id": "marketing.lead@x.com"},
        ).status_code == 200
    return campaign_id


def test_meta_exposes_repurposing_agent(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["repurposing"]["agent_id"] == "content_repurposing"
    assert meta["repurposing"]["model"] == "claude-opus-5"
    assert any(r["asset_type"] == "faq_service_page" for r in meta["repurposing"]["recipes"])


def test_flagship_requires_confirmed_plan(client: TestClient) -> None:
    outcome = client.post(
        "/api/box/campaigns/cmp_nope/flagship", json={"actor_id": "studio@x.com"}
    ).json()
    assert outcome["status"] == "failed"
    assert outcome["escalation_reasons"] == ["plan_not_ready"]


def test_drafts_for_unknown_campaign_is_404(client: TestClient) -> None:
    """A campaign from a previous bridge session must 404 (the studio stops its
    poll on 404) — never a 200 with empty state."""
    assert client.get("/api/box/campaigns/cmp_gone/drafts").status_code == 404


def test_full_repurposing_lifecycle_over_http(client: TestClient) -> None:
    campaign_id = _campaign_in_production(client)

    # Flagship draft on outline approval.
    drafted = client.post(
        f"/api/box/campaigns/{campaign_id}/flagship", json={"actor_id": "studio@x.com"}
    )
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["status"] == "flagship_staged"

    # Drafts endpoint serves workspace-relative refs; the docx downloads.
    drafts = client.get(f"/api/box/campaigns/{campaign_id}/drafts").json()
    assert drafts["status"] == "flagship_staged"
    flagship = next(d for d in drafts["drafts"] if d["kind"] == "flagship")
    assert flagship["file_rel"]
    doc = client.get(f"/api/box/documents?path={flagship['file_rel']}")
    assert doc.status_code == 200 and doc.content[:2] == b"PK"

    # Fan-out before the human confirmation is refused (sequencing guardrail).
    refused = client.post(f"/api/box/campaigns/{campaign_id}/fanout")
    assert refused.status_code == 409

    # Human confirm: records the gate AND registers the REAL flagship bytes with
    # Agent 2's packaging registry, with claim lineage.
    confirmed = client.post(
        f"/api/box/campaigns/{campaign_id}/flagship/confirm",
        json={"actor_id": "jen.cook@levelshift.com", "actor_role": "content-writer"},
    )
    assert confirmed.status_code == 200, confirmed.text
    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    registered = {a["asset_id"]: a for a in detail["registered_assets"]}
    assert "flagship_blog" in registered
    assert registered["flagship_blog"]["claim_refs"] == ["brief:offer_topic"]

    # Fan-out from the confirmed flagship.
    fanout = client.post(f"/api/box/campaigns/{campaign_id}/fanout")
    assert fanout.status_code == 200, fanout.text
    body = fanout.json()
    assert body["status"] == "derivatives_staged"
    staged_types = {d["asset_type"] for d in body["staged"]}
    # Required create-assets with channel recipes; flagship excluded.
    assert "flagship_blog" not in staged_types
    assert {"linkedin_posts", "email_touchpoints", "faq_service_page"} <= staged_types
    assert all(d["claim_lineage"] == ["cl-1"] for d in body["staged"])
    assert body["inventory"]["method"] == "llm_verified"

    # Per-asset confirm now registers Agent 3's REAL bytes (docx, not stand-in).
    checklist_ids = [i["asset_id"] for i in detail["checklist"]["items"]]
    for asset_id in checklist_ids:
        if asset_id == "flagship_blog":
            continue
        response = client.post(
            f"/api/box/campaigns/{campaign_id}/assets/{asset_id}/confirm",
            json={"actor_id": "reviewer@x.com"},
        )
        assert response.status_code == 200, response.text
    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    linkedin = next(
        a for a in detail["registered_assets"] if a["asset_id"] == "linkedin_posts"
    )
    assert linkedin["claim_refs"] == ["cl-1"]  # lineage from the real draft

    # Packaging completes with the real drafts.
    packaged = client.post(f"/api/box/campaigns/{campaign_id}/package").json()
    assert packaged["status"] == "packaged_pending_compliance"
    assert packaged["manifest"]["claim_lineage_index"]["linkedin_posts"] == ["cl-1"]

    # Rework one derivative over HTTP: new version, others untouched.
    rework = client.post(
        f"/api/box/campaigns/{campaign_id}/rework",
        json={"asset_id": "linkedin_posts", "instruction": "shorter hooks",
              "actor_id": "rishi@x.com"},
    )
    assert rework.status_code == 200, rework.text
    assert rework.json()["draft"]["version"] == 2


def test_repurposing_telemetry_shares_the_campaign_trace(client: TestClient) -> None:
    campaign_id = _campaign_in_production(client)
    client.post(f"/api/box/campaigns/{campaign_id}/flagship",
                json={"actor_id": "studio@x.com"})
    telemetry = client.get("/api/telemetry").json()
    rp_traces = {
        r["shiftai.trace.id"] for r in telemetry
        if r["shiftai.agent.id"] == "content_repurposing"
    }
    box_traces = {
        r["shiftai.trace.id"] for r in telemetry
        if r["shiftai.agent.id"] == "campaign_in_a_box"
    }
    assert rp_traces and rp_traces <= box_traces


def test_kill_switch_pauses_repurposing_agent_too(client: TestClient) -> None:
    campaign_id = _campaign_in_production(client)
    client.post(f"/api/box/campaigns/{campaign_id}/flagship",
                json={"actor_id": "studio@x.com"})
    client.post(f"/api/box/campaigns/{campaign_id}/flagship/confirm",
                json={"actor_id": "jen@x.com"})
    client.post("/api/control/kill-switch", json={"paused": True, "reason": "drill"})
    outcome = client.post(f"/api/box/campaigns/{campaign_id}/fanout").json()
    assert outcome["escalation_reasons"] == ["control_pause"]
    client.post("/api/control/kill-switch", json={"paused": False})
    assert client.post(
        f"/api/box/campaigns/{campaign_id}/fanout"
    ).json()["status"] == "derivatives_staged"
