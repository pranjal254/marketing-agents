"""Step 9: completeness diff against the asset checklist.

A non-empty diff BLOCKS packaging with an actionable report — it is never padded
or trimmed to fit (guardrail 4). Also verifies every asset carries a human
confirmation record.
"""

from __future__ import annotations

from c2c_campaign_box.models import AssetChecklistItem, CompletenessDiff, RegisteredAsset


def completeness_diff(
    checklist_items: list[AssetChecklistItem],
    registered: list[RegisteredAsset],
) -> CompletenessDiff:
    expected_ids = {i.asset_id for i in checklist_items}
    by_id = {a.asset_id: a for a in registered}

    missing = sorted(
        asset_id
        for asset_id in expected_ids
        if asset_id not in by_id or by_id[asset_id].status not in ("content_confirmed", "packaged")
    )
    extra = sorted(a.asset_id for a in registered if a.asset_id not in expected_ids)
    version_mismatch = sorted(
        a.asset_id
        for a in registered
        if a.asset_id in expected_ids
        and a.confirmation is not None
        and "version" in a.confirmation.deltas
        and int(a.confirmation.deltas["version"]) != a.version
    )
    return CompletenessDiff(missing=missing, extra=extra, version_mismatch=version_mismatch)


def missing_confirmation_records(registered: list[RegisteredAsset]) -> list[str]:
    """Any packaged asset without a human confirmation record halts the package
    (spec Alerting)."""
    return sorted(
        a.asset_id
        for a in registered
        if a.status == "content_confirmed"
        and (a.confirmation is None or a.confirmation.decision != "confirmed")
    )
