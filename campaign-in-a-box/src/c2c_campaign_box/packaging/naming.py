"""Step 10 (naming half): naming convention + required metadata per asset.

Auto-corrects ONLY unambiguous cases (case/spacing/underscore variants of the
expected canonical name); anything else is flagged for human review
(naming_ambiguous — a spec Human Review Trigger), never guessed.
"""

from __future__ import annotations

import re

from c2c_campaign_box.agent_config import OrchestratorConfig
from c2c_campaign_box.models import NamingIssue, RegisteredAsset
from c2c_campaign_box.workspace import asset_filename


def _canonical(config: OrchestratorConfig, campaign_slug: str, asset: RegisteredAsset) -> str:
    stem = asset_filename(config, campaign_slug, asset.asset_type, asset.version)
    ext = _extension(asset.filename)
    return f"{stem}{ext}"


def _extension(filename: str) -> str:
    match = re.search(r"(\.[A-Za-z0-9]+)$", filename)
    return match.group(1).lower() if match else ""


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", name.lower()).strip("-")


def validate_names(
    config: OrchestratorConfig,
    campaign_slug: str,
    registered: list[RegisteredAsset],
) -> tuple[dict[str, str], list[NamingIssue]]:
    """Returns (asset_id → canonical packaged name, issues).

    - exact match → no issue;
    - same after normalization (case/spacing/underscores) → auto_corrected;
    - anything else → flagged (blocks packaging until a human resolves it).
    """
    canonical_names: dict[str, str] = {}
    issues: list[NamingIssue] = []
    for asset in registered:
        expected = _canonical(config, campaign_slug, asset)
        canonical_names[asset.asset_id] = expected
        if asset.filename == expected:
            continue
        if _normalize(asset.filename) == _normalize(expected):
            issues.append(
                NamingIssue(
                    asset_id=asset.asset_id,
                    given=asset.filename,
                    expected=expected,
                    resolution="auto_corrected",
                )
            )
        else:
            issues.append(
                NamingIssue(
                    asset_id=asset.asset_id,
                    given=asset.filename,
                    expected=expected,
                    resolution="flagged",
                )
            )
    return canonical_names, issues


def flagged_issues(issues: list[NamingIssue]) -> list[NamingIssue]:
    return [i for i in issues if i.resolution == "flagged"]
