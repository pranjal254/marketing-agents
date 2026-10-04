"""Per-campaign content settings: the clamping rules, the fallbacks, and the
fact that a saved setting actually reaches generation.

These use the SHIPPED config rather than the widened test fixture, because the
whole point is what the configured ceiling and word ranges do.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from c2c_campaign_box.models import AssetChecklist, AssetChecklistItem
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.context_store.local_store import InMemoryContextStore

from c2c_content_repurposing import persistence as db
from c2c_content_repurposing.agent_config import (
    RepurposingConfig,
    load_repurposing_config,
)
from c2c_content_repurposing.content_settings import (
    apply_requests,
    defaults_for_checklist,
    resolve,
)
from c2c_content_repurposing.fanout import build_fanout_jobs
from c2c_content_repurposing.selfcheck import failure_feedback, run_self_check

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "content_repurposing.json"
CAMPAIGN = "cmp_settings_test"


@pytest.fixture()
def shipped() -> RepurposingConfig:
    return load_repurposing_config(CONFIG_PATH)


def _item(asset_type: str, volume: int = 1) -> AssetChecklistItem:
    return AssetChecklistItem(
        asset_id=asset_type,
        asset_type=asset_type,
        label=asset_type.replace("_", " ").title(),
        volume=volume,
        decision="create",
        decision_rationale="test",
    )


def _checklist(*items: AssetChecklistItem) -> AssetChecklist:
    return AssetChecklist(
        campaign_id=CAMPAIGN, version=1, items=list(items), search_performed=True
    )


# --------------------------------------------------------------- defaults


def test_defaults_come_from_the_plan_volume_and_the_config_range(
    shipped: RepurposingConfig,
) -> None:
    settings = defaults_for_checklist(
        CAMPAIGN, [_item("linkedin_posts", 4), _item("flagship_blog", 1)], shipped
    )
    linkedin = settings.for_asset("linkedin_posts")
    assert linkedin is not None
    assert linkedin.variants == 4
    assert (linkedin.min_words, linkedin.max_words) == (60, 150)

    flagship = settings.for_asset("flagship_blog")
    assert flagship is not None
    assert (flagship.min_words, flagship.max_words) == (900, 1400)


def test_a_plan_volume_above_the_ceiling_is_clamped_in_the_defaults(
    shipped: RepurposingConfig,
) -> None:
    settings = defaults_for_checklist(CAMPAIGN, [_item("linkedin_posts", 9)], shipped)
    item = settings.for_asset("linkedin_posts")
    assert item is not None
    assert item.variants == shipped.content_limits.max_variants_per_asset == 5


def test_an_asset_type_with_no_configured_range_falls_back_to_the_default(
    shipped: RepurposingConfig,
) -> None:
    settings = defaults_for_checklist(CAMPAIGN, [_item("brand_new_type", 1)], shipped)
    item = settings.for_asset("brand_new_type")
    assert item is not None
    expected = shipped.content_limits.default_word_range
    assert (item.min_words, item.max_words) == (expected.min_words, expected.max_words)


# --------------------------------------------------------------- clamping


def test_requesting_more_than_the_ceiling_is_clamped_and_reported(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(CAMPAIGN, [_item("email_touchpoints", 3)], shipped)
    saved = apply_requests(
        CAMPAIGN, 2,
        [{"asset_id": "email_touchpoints", "variants": 12}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    item = saved.for_asset("email_touchpoints")
    assert item is not None and item.variants == 5
    assert any("ceiling is 5" in note for note in saved.adjustments)


def test_word_counts_outside_the_allowed_bounds_are_clamped_and_reported(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(CAMPAIGN, [_item("flagship_blog")], shipped)
    saved = apply_requests(
        CAMPAIGN, 2,
        [{"asset_id": "flagship_blog", "min_words": 1, "max_words": 99_999}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    item = saved.for_asset("flagship_blog")
    assert item is not None
    assert item.min_words == shipped.content_limits.word_floor
    assert item.max_words == shipped.content_limits.word_ceiling
    assert any("adjusted" in note for note in saved.adjustments)


def test_an_inverted_range_is_corrected_rather_than_refused(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(CAMPAIGN, [_item("call_scripts")], shipped)
    saved = apply_requests(
        CAMPAIGN, 2,
        [{"asset_id": "call_scripts", "min_words": 400, "max_words": 100}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    item = saved.for_asset("call_scripts")
    assert item is not None
    assert item.min_words == 400 and item.max_words == 400
    assert any("below the minimum" in note for note in saved.adjustments)


def test_a_partial_update_leaves_every_other_asset_alone(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(
        CAMPAIGN, [_item("linkedin_posts", 4), _item("call_scripts", 1)], shipped
    )
    saved = apply_requests(
        CAMPAIGN, 2,
        [{"asset_id": "linkedin_posts", "variants": 2}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    assert saved.for_asset("linkedin_posts").variants == 2  # type: ignore[union-attr]
    untouched = saved.for_asset("call_scripts")
    assert untouched is not None
    assert (untouched.variants, untouched.min_words) == (1, 150)


def test_an_asset_not_on_the_plan_is_ignored_not_invented(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(CAMPAIGN, [_item("call_scripts")], shipped)
    saved = apply_requests(
        CAMPAIGN, 2,
        [{"asset_id": "whitepaper", "variants": 3}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    assert saved.for_asset("whitepaper") is None
    assert any("not on the approved asset plan" in note for note in saved.adjustments)


# --------------------------------------------------------------- reaching generation


def test_saved_settings_drive_the_fanout_jobs(shipped: RepurposingConfig) -> None:
    checklist = _checklist(_item("linkedin_posts", 4), _item("email_touchpoints", 3))
    baseline = defaults_for_checklist(CAMPAIGN, list(checklist.items), shipped)
    saved = apply_requests(
        CAMPAIGN, 2,
        [
            {"asset_id": "linkedin_posts", "variants": 2, "min_words": 100, "max_words": 140},
            {"asset_id": "email_touchpoints", "variants": 5},
        ],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )

    jobs, _ = build_fanout_jobs(checklist, shipped, saved)
    by_asset = {job.asset_id: job for job in jobs}
    assert by_asset["linkedin_posts"].volume == 2
    assert (by_asset["linkedin_posts"].min_words, by_asset["linkedin_posts"].max_words) == (
        100,
        140,
    )
    assert by_asset["email_touchpoints"].volume == 5


def test_without_saved_settings_the_jobs_fall_back_to_plan_and_config(
    shipped: RepurposingConfig,
) -> None:
    checklist = _checklist(_item("linkedin_posts", 4))
    jobs, _ = build_fanout_jobs(checklist, shipped, None)
    assert jobs[0].volume == 4
    assert (jobs[0].min_words, jobs[0].max_words) == (60, 150)


def test_an_asset_added_after_settings_were_saved_still_resolves(
    shipped: RepurposingConfig,
) -> None:
    baseline = defaults_for_checklist(CAMPAIGN, [_item("call_scripts")], shipped)
    chosen = resolve(baseline, "battle_card", "battle_card", 1, shipped)
    assert (chosen.min_words, chosen.max_words) == (150, 350)


# --------------------------------------------------------------- persistence


def test_every_save_is_a_new_version_and_history_is_readable(
    shipped: RepurposingConfig,
) -> None:
    store = InMemoryContextStore()
    baseline = defaults_for_checklist(CAMPAIGN, [_item("linkedin_posts", 4)], shipped)
    db.save_content_settings(store, baseline)
    second = apply_requests(
        CAMPAIGN, 2, [{"asset_id": "linkedin_posts", "variants": 2}],
        baseline, shipped, set_by="u_writer", set_by_role="content-writer",
    )
    db.save_content_settings(store, second)

    latest = db.load_content_settings(store, CAMPAIGN)
    assert latest is not None and latest.version == 2
    assert latest.set_by == "u_writer"

    history = db.content_settings_history(store, CAMPAIGN)
    assert [h.version for h in history] == [1, 2]


# --------------------------------------------------------------- self-check


def test_self_check_flags_a_draft_that_is_too_short() -> None:
    rules = load_brand_rules("levelshift")
    report = run_self_check(
        "far too short", rules, unsourced_numeric_tokens=[], word_range=(900, 1400)
    )
    assert report.passed is False
    assert report.word_count == 3
    assert report.word_count_in_range is False
    feedback = " ".join(failure_feedback(report))
    assert "word_count_out_of_range" in feedback
    assert "897 too short" in feedback


def test_self_check_flags_a_draft_that_is_too_long() -> None:
    rules = load_brand_rules("levelshift")
    report = run_self_check(
        " ".join(["word"] * 200), rules, unsourced_numeric_tokens=[], word_range=(60, 150)
    )
    assert report.word_count_in_range is False
    assert "50 too long" in " ".join(failure_feedback(report))


def test_self_check_passes_inside_the_range_and_skips_the_check_without_one() -> None:
    rules = load_brand_rules("levelshift")
    inside = run_self_check(
        " ".join(["word"] * 100), rules, unsourced_numeric_tokens=[], word_range=(60, 150)
    )
    assert inside.passed is True and inside.word_count_in_range is True

    unchecked = run_self_check(" ".join(["word"] * 3), rules, unsourced_numeric_tokens=[])
    assert unchecked.passed is True
    assert unchecked.word_range is None
    assert unchecked.word_count == 3
