"""Steps 10-11 (snapshot half): hashed final-version snapshots, planned then
committed transactionally.

Two-phase design keeps partial manifests impossible:
  PLAN (pure, no side effects): download every confirmed asset's bytes, compute
  sha256 per asset, build the full snapshot plan. Any read failure aborts before
  a single write happens.
  COMMIT (in orchestration): upload every snapshot copy into final/; on a write
  failure mid-commit the manifest is never registered, state reverts, and the
  orphaned refs are recorded for human cleanup (agents never delete — guardrail).
"""

from __future__ import annotations

from dataclasses import dataclass

from shiftai_shared.hashing import sha256_hex

from c2c_campaign_box.models import RegisteredAsset
from c2c_campaign_box.workspace import CampaignWorkspace


class SnapshotReadError(Exception):
    """A confirmed asset's bytes could not be read — packaging aborts pre-write."""


@dataclass(frozen=True)
class SnapshotPlanItem:
    asset_id: str
    asset_type: str
    source_ref: str
    canonical_name: str
    version: int
    sha256: str
    content: bytes


def plan_snapshots(
    workspace: CampaignWorkspace,
    registered: list[RegisteredAsset],
    canonical_names: dict[str, str],
) -> list[SnapshotPlanItem]:
    """Pure read phase: fetch bytes + hash for every asset. No writes."""
    plan: list[SnapshotPlanItem] = []
    for asset in sorted(registered, key=lambda a: a.asset_id):
        try:
            content = workspace.download(asset.file_ref)
        except Exception as exc:
            raise SnapshotReadError(
                f"cannot read confirmed asset {asset.asset_id!r} ({asset.file_ref}): {exc}"
            ) from exc
        plan.append(
            SnapshotPlanItem(
                asset_id=asset.asset_id,
                asset_type=asset.asset_type,
                source_ref=asset.file_ref,
                canonical_name=canonical_names[asset.asset_id],
                version=asset.version,
                sha256=sha256_hex(content),
                content=content,
            )
        )
    return plan


def verify_rehash(item: SnapshotPlanItem, current_bytes: bytes) -> bool:
    """Re-entry check: a packaged asset whose bytes changed since snapshot is a
    hash mismatch → halt package, escalate to AiCoE."""
    return sha256_hex(current_bytes) == item.sha256
