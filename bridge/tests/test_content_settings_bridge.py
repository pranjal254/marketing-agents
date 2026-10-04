"""Content settings over HTTP: defaults before anything is saved, clamping at
the configured ceiling, versioning, and the settings actually reaching the
fan-out rather than sitting in the store unread.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_repurpose_bridge import _campaign_in_production
from tests.test_repurpose_bridge import client as client

WRITER = "u_writer"


@pytest.fixture()
def confirmed_flagship(client: TestClient) -> str:
    """A campaign at exactly the point the Content Writer sets these: plan
    confirmed, flagship drafted and confirmed, fan-out not yet run."""
    campaign_id = _campaign_in_production(client)
    assert (
        client.post(
            f"/api/box/campaigns/{campaign_id}/flagship",
            json={"actor_id": "studio@x.com"},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/box/campaigns/{campaign_id}/flagship/confirm",
            json={"actor_id": "jen.cook@levelshift.com", "actor_role": "content-writer"},
        ).status_code
        == 200
    )
    return campaign_id


def _settings(client: TestClient, campaign_id: str) -> dict[str, Any]:
    response = client.get(f"/api/box/campaigns/{campaign_id}/content-settings")
    assert response.status_code == 200, response.text
    return response.json()


def _save(client: TestClient, campaign_id: str, items: list[dict]) -> dict[str, Any]:
    response = client.put(
        f"/api/box/campaigns/{campaign_id}/content-settings",
        json={"actor_id": WRITER, "actor_role": "content-writer", "items": items},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _by_asset(view: dict[str, Any]) -> dict[str, dict]:
    return {item["asset_id"]: item for item in view["settings"]["items"]}


def test_defaults_are_served_before_anything_is_saved(
    client: TestClient, confirmed_flagship: str
) -> None:
    view = _settings(client, confirmed_flagship)
    assert view["saved"] is False
    assert view["limits"]["max_variants_per_asset"] == 5

    items = _by_asset(view)
    assert "flagship_blog" in items
    assert (items["flagship_blog"]["min_words"], items["flagship_blog"]["max_words"]) == (
        900,
        1400,
    )


def test_settings_for_an_unknown_campaign_are_a_404_not_an_empty_list(
    client: TestClient,
) -> None:
    response = client.get("/api/box/campaigns/cmp_does_not_exist/content-settings")
    assert response.status_code == 404


def test_saving_returns_the_clamped_values_with_an_explanation(
    client: TestClient, confirmed_flagship: str
) -> None:
    view = _save(
        client,
        confirmed_flagship,
        [{"asset_id": "linkedin_posts", "variants": 11, "min_words": 90, "max_words": 130}],
    )
    assert view["saved"] is True
    item = _by_asset(view)["linkedin_posts"]
    assert item["variants"] == 5
    assert (item["min_words"], item["max_words"]) == (90, 130)
    assert any("ceiling is 5" in note for note in view["settings"]["adjustments"])


def test_each_save_is_a_new_identity_stamped_version(
    client: TestClient, confirmed_flagship: str
) -> None:
    first = _save(client, confirmed_flagship, [{"asset_id": "linkedin_posts", "variants": 2}])
    assert first["settings"]["version"] == 1
    assert first["settings"]["set_by"] == WRITER

    second = _save(client, confirmed_flagship, [{"asset_id": "call_scripts", "variants": 3}])
    assert second["settings"]["version"] == 2
    # The earlier change is still in effect; a save is a patch, not a replace.
    items = _by_asset(second)
    assert items["linkedin_posts"]["variants"] == 2
    assert items["call_scripts"]["variants"] == 3


def test_saved_settings_survive_a_reread(
    client: TestClient, confirmed_flagship: str
) -> None:
    _save(client, confirmed_flagship, [{"asset_id": "email_touchpoints", "variants": 4}])
    view = _settings(client, confirmed_flagship)
    assert view["saved"] is True
    assert _by_asset(view)["email_touchpoints"]["variants"] == 4


def test_the_fanout_honours_the_saved_variant_count(
    client: TestClient, confirmed_flagship: str
) -> None:
    """The settings are only worth anything if generation reads them."""
    _save(client, confirmed_flagship, [{"asset_id": "linkedin_posts", "variants": 2}])

    response = client.post(f"/api/box/campaigns/{confirmed_flagship}/fanout")
    assert response.status_code == 200, response.text

    drafts = client.get(f"/api/box/campaigns/{confirmed_flagship}/drafts").json()
    linkedin = [d for d in drafts["drafts"] if d["asset_id"] == "linkedin_posts"]
    assert linkedin, "the fan-out produced no LinkedIn derivative"
    # The mock provider returns a fixed set of variants; the volume cap trims it
    # to what the writer asked for.
    assert len(linkedin[-1]["sections"]) <= 2
