"""Agent 5 bridge endpoints — the terminal mile over HTTP: package manifest →
gate (deterministic + contextual) → sequenced human approvals → lock + release;
returns re-open packaging and the review cycle with reviewer notes verbatim."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app
from tests.test_box_bridge import PACK_JSON, REUSE_JSON
from tests.test_bridge import CLASSIFY_OK, COMPLETE_REQUEST
from tests.test_repurpose_bridge import DERIVATIVE_JSON, FLAGSHIP_JSON, INVENTORY_JSON

GATE_CLEAN_JSON = json.dumps({
    "findings": [],
    "checks_completed": [
        "bc_fo_meaning", "brand_voice", "claim_sourcing",
        "copilot_scope", "tone_urgency_fear",
    ],
    "confidence": 0.9,
})


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from shiftai_shared.llm import LLMResponse, MockLLMProvider

    import c2c_bridge.app as app_mod

    class EchoingProvider(MockLLMProvider):
        def complete(self, *, system, user, model, max_tokens,  # type: ignore[no-untyped-def]
                     temperature=0.0, timeout_s=60.0):
            if user.startswith("Consolidate this round's reviewer feedback"):
                payload = json.loads(
                    user.rsplit("<case_data>", 1)[1].split("</case_data>", 1)[0]
                )
                items = [
                    {"feedback_id": i["feedback_id"], "location": i.get("section", ""),
                     "instruction": i["text"], "reviewer": i.get("reviewer", ""),
                     "type": "textual", "rationale": "copy edit"}
                    for i in payload["feedback_items"]
                ]
                return LLMResponse(text=json.dumps({"items": items, "confidence": 0.9}),
                                   model=self.model_name, input_tokens=10, output_tokens=10)
            if user.startswith("Apply ONLY the textual edits"):
                payload = json.loads(
                    user.rsplit("<case_data>", 1)[1].split("</case_data>", 1)[0]
                )
                return LLMResponse(text=json.dumps({
                    "sections": payload["current_sections"],
                    "applied": [e["feedback_id"] for e in payload["edits_to_apply"]],
                    "deferred": [], "edit_summary": "Applied.", "confidence": 0.9,
                }), model=self.model_name, input_tokens=10, output_tokens=10)
            return super().complete(system=system, user=user, model=model,
                                    max_tokens=max_tokens, temperature=temperature,
                                    timeout_s=timeout_s)

    def fake_build_provider(_: SharedSettings) -> MockLLMProvider:
        return EchoingProvider(
            default=CLASSIFY_OK,
            script=[
                (lambda u: "audience & offer pack" in u, PACK_JSON),
                (lambda u: "reuse / adapt / create" in u, REUSE_JSON),
                (lambda u: "Draft the flagship asset" in u, FLAGSHIP_JSON),
                (lambda u: u.startswith("Extract the confirmed flagship's claim inventory"),
                 INVENTORY_JSON),
                (lambda u: "derivative from the claim inventory" in u, DERIVATIVE_JSON),
                (lambda u: u.startswith("Run the contextual compliance pass"),
                 GATE_CLEAN_JSON),
            ],
        )

    monkeypatch.setattr(app_mod, "build_provider", fake_build_provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _packaged_campaign(client: TestClient) -> str:
    """Drive the whole pipeline: intake → plan → confirms → flagship → fan-out →
    confirm every checklist asset → packaging manifest."""
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
        client.post(f"/api/box/campaigns/{campaign_id}/confirm",
                    json={"kind": kind, "actor_id": "marketing.lead@x.com"})
    assert client.post(
        f"/api/box/campaigns/{campaign_id}/flagship", json={"actor_id": "studio@x.com"}
    ).status_code == 200
    assert client.post(
        f"/api/box/campaigns/{campaign_id}/flagship/confirm",
        json={"actor_id": "jen@x.com", "actor_role": "content-writer"},
    ).status_code == 200
    assert client.post(f"/api/box/campaigns/{campaign_id}/fanout").status_code == 200
    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    for item in detail["checklist"]["items"]:
        if item["asset_id"] == "flagship_blog":
            continue
        confirmed = client.post(
            f"/api/box/campaigns/{campaign_id}/assets/{item['asset_id']}/confirm",
            json={"actor_id": "jen@x.com"},
        )
        assert confirmed.status_code == 200, confirmed.text
    packaged = client.post(f"/api/box/campaigns/{campaign_id}/package")
    assert packaged.status_code == 200, packaged.text
    assert packaged.json()["manifest"] is not None
    return campaign_id


def test_meta_exposes_quality_gate(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    gate = meta["quality_gate"]
    assert gate["agent_id"] == "quality_gate_approval"
    assert gate["model"] == "claude-sonnet-5"
    assert gate["distribution_classes"]["battle_card"] == "internal"
    assert gate["package_signoff"]["role"] == "bu-campaign-lead"


def test_gate_requires_a_manifest(client: TestClient) -> None:
    assert client.post("/api/box/campaigns/cmp_none/gate/run").status_code == 409


def test_gate_to_locked_package_over_http(client: TestClient) -> None:
    campaign_id = _packaged_campaign(client)

    run = client.post(f"/api/box/campaigns/{campaign_id}/gate/run")
    assert run.status_code == 200, run.text
    outcome = run.json()
    assert outcome["status"] == "in_review", outcome["reports"]
    assert outcome["failed_asset_ids"] == []

    detail = client.get(f"/api/box/campaigns/{campaign_id}/gate").json()
    assert detail["state"]["status"] == "in_review"
    assert all(r["verdict"] == "pass" for r in detail["reports"])
    assert all(r["checks_complete"] for r in detail["reports"])

    # Sequenced human approvals: every asset review first, then the BU Lead.
    package_task = next(t for t in detail["tasks"] if t["scope"] == "package")
    early = client.post(
        f"/api/box/campaigns/{campaign_id}/gate/tasks/{package_task['task_id']}/decision",
        json={"decision": "approved", "actor_id": "lead@x.com",
              "actor_role": "bu-campaign-lead"},
    )
    assert early.status_code == 409  # structural sequence integrity

    for task in detail["tasks"]:
        if task["scope"] == "asset" and task["status"] == "open":
            decided = client.post(
                f"/api/box/campaigns/{campaign_id}/gate/tasks/{task['task_id']}/decision",
                json={"decision": "approved", "actor_id": "qa@x.com",
                      "actor_role": task["role"]},
            )
            assert decided.status_code == 200, decided.text
    signoff = client.post(
        f"/api/box/campaigns/{campaign_id}/gate/tasks/{package_task['task_id']}/decision",
        json={"decision": "approved", "actor_id": "lead@x.com",
              "actor_role": "bu-campaign-lead"},
    )
    assert signoff.status_code == 200, signoff.text

    final = client.get(f"/api/box/campaigns/{campaign_id}/gate").json()
    assert final["state"]["status"] == "approved_locked"
    assert final["state"]["locks"]
    for lock in final["state"]["locks"]:
        assert Path(lock["final_ref"]).is_file()
        assert "final" in Path(lock["final_ref"]).parts
    # the chain has an identity + hash for every decision, package sign-off included
    assert all(a["actor_id"] and a["sha256"] for a in final["approvals"])
    assert any(a["scope"] == "package" for a in final["approvals"])
    # a locked package refuses re-gating
    assert client.post(f"/api/box/campaigns/{campaign_id}/gate/run").status_code == 409
    # lock integrity endpoint
    verify = client.post(f"/api/box/campaigns/{campaign_id}/gate/verify-locks")
    assert verify.status_code == 200 and verify.json()["violated"] == []


def test_review_return_reopens_packaging_and_review(client: TestClient) -> None:
    campaign_id = _packaged_campaign(client)
    client.post(f"/api/box/campaigns/{campaign_id}/gate/run")
    detail = client.get(f"/api/box/campaigns/{campaign_id}/gate").json()
    task = next(
        t for t in detail["tasks"] if t["scope"] == "asset" and t["status"] == "open"
    )
    returned = client.post(
        f"/api/box/campaigns/{campaign_id}/gate/tasks/{task['task_id']}/decision",
        json={"decision": "returned", "actor_id": "qa@x.com",
              "actor_role": "grammar-quality-reviewer",
              "notes": "Grammar issues in the second paragraph.",
              "disputed_rule_ids": []},
    )
    assert returned.status_code == 200, returned.text
    gate_state = client.get(f"/api/box/campaigns/{campaign_id}/gate").json()["state"]
    assert gate_state["status"] == "returned"
    # packaging case re-opened for the returned asset
    box = client.get(f"/api/box/campaigns/{campaign_id}").json()
    assert box["summary"]["status"] == "in_production"
    # the reviewer's notes travel verbatim into the review cycle
    review = client.get(f"/api/box/campaigns/{campaign_id}/review").json()
    asset = next(a for a in review["assets"] if a["state"]["asset_id"] == task["asset_id"])
    assert asset["state"]["status"] != "content_confirmed"
    assert any(
        f["text"] == "Grammar issues in the second paragraph." for f in asset["feedback"]
    )
