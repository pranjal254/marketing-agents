"""Agent 4 bridge endpoints — the review cycle over HTTP: staged drafts enter
review, reviewers comment, a round consolidates/holds/applies, the Marketing Lead
resolves conflicts, and the human confirm drives fan-out + packaging through the
signal bindings (the old stand-ins are gone)."""

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

CONSOLIDATION_JSON = json.dumps(
    {
        "items": [
            {"feedback_id": "PLACEHOLDER", "location": "Problem",
             "instruction": "Tighten the opening", "reviewer": "jen",
             "type": "textual", "rationale": "copy edit"},
        ],
        "confidence": 0.9,
    }
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from shiftai_shared.llm import LLMResponse, MockLLMProvider

    import c2c_bridge.app as app_mod

    class EchoingProvider(MockLLMProvider):
        """Static scripts for agents 1-3; echo-based replies for Agent 4 (its
        contracts must reference the REAL generated feedback ids)."""

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
                text = json.dumps({"items": items, "confidence": 0.9})
                return LLMResponse(text=text, model=self.model_name,
                                   input_tokens=10, output_tokens=10)
            if user.startswith("Apply ONLY the textual edits"):
                payload = json.loads(
                    user.rsplit("<case_data>", 1)[1].split("</case_data>", 1)[0]
                )
                sections = payload["current_sections"]
                applied = [e["feedback_id"] for e in payload["edits_to_apply"]]
                text = json.dumps({
                    "sections": sections,  # unchanged → markers trivially survive
                    "applied": applied, "deferred": [],
                    "edit_summary": "Kept structure; applied the copy edits.",
                    "confidence": 0.9,
                })
                return LLMResponse(text=text, model=self.model_name,
                                   input_tokens=10, output_tokens=10)
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
            ],
        )

    monkeypatch.setattr(app_mod, "build_provider", fake_build_provider)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _campaign_with_flagship(client: TestClient) -> str:
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
    assert client.post(
        f"/api/box/campaigns/{campaign_id}/flagship", json={"actor_id": "studio@x.com"}
    ).status_code == 200
    return campaign_id


def test_meta_exposes_collaboration_agent(client: TestClient) -> None:
    meta = client.get("/api/meta").json()
    assert meta["collaboration"]["agent_id"] == "collaboration_iteration"
    assert meta["collaboration"]["model"] == "claude-sonnet-5"
    assert meta["collaboration"]["reviewer_map"]["flagship"]


def test_staged_flagship_enters_review_automatically(client: TestClient) -> None:
    campaign_id = _campaign_with_flagship(client)
    review = client.get(f"/api/box/campaigns/{campaign_id}/review").json()
    assets = {a["state"]["asset_id"]: a for a in review["assets"]}
    assert assets["flagship_blog"]["state"]["status"] == "in_review"
    assert {r["role"] for r in assets["flagship_blog"]["state"]["reviewers"]} == {
        "content-writer", "marketing-lead",
    }


def test_full_review_cycle_over_http(client: TestClient) -> None:
    campaign_id = _campaign_with_flagship(client)

    # A reviewer comments, then signals feedback-complete → a round runs.
    fb = client.post(
        f"/api/box/campaigns/{campaign_id}/assets/flagship_blog/feedback",
        json={"reviewer_id": "jen.cook@levelshift.com",
              "reviewer_role": "content-writer",
              "section": "Problem", "text": "Tighten the opening"},
    )
    assert fb.status_code == 200, fb.text
    round_out = client.post(
        f"/api/box/campaigns/{campaign_id}/assets/flagship_blog/feedback-complete",
        json={"actor_id": "jen.cook@levelshift.com"},
    )
    assert round_out.status_code == 200, round_out.text
    body = round_out.json()
    assert body["round"]["edit_summary"]
    assert body["round"]["resolutions"][0]["outcome"] == "applied"
    assert body["status"] == "in_revision"
    # The revised version was staged and re-entered review (v2 in the registry).
    drafts = client.get(f"/api/box/campaigns/{campaign_id}/drafts").json()["drafts"]
    flagship_versions = [d["version"] for d in drafts if d["asset_id"] == "flagship_blog"]
    assert max(flagship_versions) == 2

    # The human confirm (Agent 4 gate) unlocks fan-out and registers the bytes.
    confirmed = client.post(
        f"/api/box/campaigns/{campaign_id}/flagship/confirm",
        json={"actor_id": "jen.cook@levelshift.com", "actor_role": "content-writer"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "content_confirmed"
    detail = client.get(f"/api/box/campaigns/{campaign_id}").json()
    assert any(a["asset_id"] == "flagship_blog" for a in detail["registered_assets"])
    fanout = client.post(f"/api/box/campaigns/{campaign_id}/fanout")
    assert fanout.status_code == 200, fanout.text
    assert fanout.json()["status"] == "derivatives_staged"

    # Derivatives entered review; confirm one via the per-asset gate.
    review = client.get(f"/api/box/campaigns/{campaign_id}/review").json()
    statuses = {a["state"]["asset_id"]: a["state"]["status"] for a in review["assets"]}
    assert statuses.get("linkedin_posts") == "in_review"
    confirm_asset = client.post(
        f"/api/box/campaigns/{campaign_id}/assets/linkedin_posts/confirm",
        json={"actor_id": "jen.cook@levelshift.com"},
    )
    assert confirm_asset.status_code == 200, confirm_asset.text
    assert confirm_asset.json()["claim_refs"] == ["cl-1"]  # REAL lineage registered

    # Sweep endpoint responds (ladder timing unit-tested in the agent package).
    sweep = client.post(f"/api/box/campaigns/{campaign_id}/sweep")
    assert sweep.status_code == 200


def test_double_confirm_is_refused(client: TestClient) -> None:
    campaign_id = _campaign_with_flagship(client)
    ok = client.post(
        f"/api/box/campaigns/{campaign_id}/flagship/confirm",
        json={"actor_id": "jen@x.com", "actor_role": "content-writer"},
    )
    assert ok.status_code == 200
    again = client.post(
        f"/api/box/campaigns/{campaign_id}/flagship/confirm",
        json={"actor_id": "jen@x.com", "actor_role": "content-writer"},
    )
    assert again.status_code == 409
