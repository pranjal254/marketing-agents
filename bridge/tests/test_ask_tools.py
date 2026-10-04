"""The tools against a real campaign, not a stub store.

The loop is only worth anything if the projections actually find the data and
survive the shapes the agents really write. These run a campaign through intake,
planning, drafting and the gate, then read it back through every tool.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from c2c_bridge import ask_tools
from tests.test_repurpose_bridge import _campaign_in_production
from tests.test_repurpose_bridge import client as client


def _store(client: TestClient) -> Any:
    return client.app.state.bridge_holder["bridge"].store


def _run(client: TestClient, tool: str, **args: Any) -> dict[str, Any]:
    return ask_tools.invoke(_store(client), tool, args)


@pytest.fixture()
def planned(client: TestClient) -> str:
    return _campaign_in_production(client)


@pytest.fixture()
def drafted(client: TestClient, planned: str) -> str:
    assert client.post(
        f"/api/box/campaigns/{planned}/flagship", json={"actor_id": "studio@x.com"}
    ).status_code == 200
    assert client.post(
        f"/api/box/campaigns/{planned}/flagship/confirm",
        json={"actor_id": "jen@x.com", "actor_role": "content-writer"},
    ).status_code == 200
    assert client.post(f"/api/box/campaigns/{planned}/fanout").status_code == 200
    return planned


# ---------------------------------------------------------------- discovery


def test_list_campaigns_finds_the_planned_campaign(
    client: TestClient, planned: str
) -> None:
    result = _run(client, "list_campaigns")
    ids = [c["campaign_id"] for c in result["campaigns"]]
    assert planned in ids
    row = next(c for c in result["campaigns"] if c["campaign_id"] == planned)
    assert row["plan_confirmed"] is True


def test_list_intake_cases_sees_the_request_behind_it(client: TestClient, planned: str) -> None:
    result = _run(client, "list_intake_cases")
    assert result["count"] >= 1
    assert any(c["campaign_id"] == planned for c in result["cases"])


# ------------------------------------------------------------------ detail


def test_get_campaign_returns_the_pack_and_the_checklist(
    client: TestClient, planned: str
) -> None:
    result = _run(client, "get_campaign", campaign_id=planned)
    assert result["status"] in {"in_production", "awaiting_confirmation"}
    assert result["audience_offer_pack"]["value_proposition"]
    assert result["audience_offer_pack"]["proof_points"]
    asset_types = {i["asset_type"] for i in result["asset_checklist"]}
    assert "flagship_blog" in asset_types
    # Every checklist row carries the decision a person would ask about.
    assert all("decision" in i for i in result["asset_checklist"])


def test_get_campaign_on_an_unknown_id_is_a_tool_error_not_a_crash(
    client: TestClient,
) -> None:
    with pytest.raises(ask_tools.ToolError, match="no campaign"):
        _run(client, "get_campaign", campaign_id="cmp_nope")


def test_get_drafts_lists_assets_with_quality_flags_but_no_prose(
    client: TestClient, drafted: str
) -> None:
    result = _run(client, "get_drafts", campaign_id=drafted)
    assert result["drafts"], "the fan-out produced nothing"
    flagship = next(d for d in result["drafts"] if d["kind"] == "flagship")
    assert flagship["self_check"]["passed"] in (True, False)
    assert "word_count" in flagship["self_check"]
    # Prose is behind its own tool; a listing must not carry it.
    assert "sections" in flagship and isinstance(flagship["sections"], int)
    assert "paragraphs" not in flagship


def test_get_draft_text_returns_the_prose_for_one_asset(
    client: TestClient, drafted: str
) -> None:
    result = _run(client, "get_draft_text", campaign_id=drafted, asset_id="flagship_blog")
    assert result["asset_id"] == "flagship_blog"
    assert result["text"].strip()
    assert result["version"] >= 1


def test_get_draft_text_for_an_undrafted_asset_says_so(
    client: TestClient, planned: str
) -> None:
    with pytest.raises(ask_tools.ToolError, match="no draft"):
        _run(client, "get_draft_text", campaign_id=planned, asset_id="flagship_blog")


def test_get_content_settings_reports_defaults_before_anything_is_saved(
    client: TestClient, drafted: str
) -> None:
    result = _run(client, "get_content_settings", campaign_id=drafted)
    assert result["saved"] is False
    assert "no settings saved" in result["note"]


def test_get_content_settings_reads_back_a_save(
    client: TestClient, drafted: str
) -> None:
    client.put(
        f"/api/box/campaigns/{drafted}/content-settings",
        json={
            "actor_id": "u_writer",
            "items": [{"asset_id": "linkedin_posts", "variants": 2}],
        },
    )
    result = _run(client, "get_content_settings", campaign_id=drafted)
    assert result["saved"] is True
    assert result["set_by"] == "u_writer"
    row = next(i for i in result["items"] if i["asset_id"] == "linkedin_posts")
    assert row["variants"] == 2


# --------------------------------------------------- not-yet-reached states


def test_tools_for_stages_not_reached_yet_explain_rather_than_fail(
    client: TestClient, planned: str
) -> None:
    """These are normal mid-journey states, so the message has to be useful."""
    with pytest.raises(ask_tools.ToolError, match="no review activity"):
        _run(client, "get_review", campaign_id=planned)
    with pytest.raises(ask_tools.ToolError, match="has not run"):
        _run(client, "get_gate", campaign_id=planned)
    with pytest.raises(ask_tools.ToolError, match="no telemetry snapshot"):
        _run(client, "get_telemetry_summary")


def test_telemetry_summary_reads_the_banked_snapshot(
    client: TestClient, planned: str
) -> None:
    client.post("/api/telemetry/export/refresh")
    result = _run(client, "get_telemetry_summary")
    assert result["totals"]["records"] > 0
    assert any(row["stage_id"] == "intake" for row in result["by_stage"])


# ------------------------------------------------------------- the contract


def test_every_registered_tool_is_callable_and_described() -> None:
    for name, tool in ask_tools.TOOLS.items():
        assert tool.name == name
        assert tool.description.strip()
        assert tool.args.startswith("{")
        assert callable(tool.run)


def test_wrong_arguments_come_back_as_a_readable_error(client: TestClient) -> None:
    with pytest.raises(ask_tools.ToolError, match="wrong arguments"):
        _run(client, "get_campaign", nonsense="x")
