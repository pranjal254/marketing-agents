"""Step 10: lock approved versions — IN PLACE, no copy.

Agent 2's packaging module already snapshots every final version into
``{campaign}/final/`` (hashed, additive); the manifest's ``snapshot_ref`` points
at that copy. Locking therefore verifies the bytes still match the manifest hash
and marks the snapshot read-only where the ref is a local path (the OneDrive
read-only lock binds here in production). The gate writes NOTHING to the
workspace — guardrail 1 holds structurally. Post-lock integrity is hash-verified
(``verify_locks`` in the orchestrator): any modification invalidates the package.
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from c2c_campaign_box.models import PackageManifest
from c2c_campaign_box.workspace import CampaignWorkspace

from c2c_quality_gate.models import LockRecord


class LockError(Exception):
    """Locking failed — the package must not release (spec Fallback)."""


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _set_read_only(ref: str) -> None:
    path = Path(ref)
    if path.is_file():  # local binding only; OneDrive refs are item ids
        path.chmod(stat.S_IREAD)


def lock_assets(workspace: CampaignWorkspace, manifest: PackageManifest) -> list[LockRecord]:
    """Hash-verify and read-only-lock every packaged snapshot in place.
    All-or-fail: the first failure aborts with LockError and the package stays
    unreleased (spec Fallback: locking failures block release)."""
    locks: list[LockRecord] = []
    for asset in manifest.assets:
        try:
            content = workspace.download(asset.snapshot_ref)
        except OSError as exc:
            raise LockError(f"cannot read snapshot for {asset.asset_id!r}: {exc}") from exc
        if sha256_hex(content) != asset.sha256:
            raise LockError(
                f"snapshot hash changed for {asset.asset_id!r} between packaging and lock"
            )
        try:
            _set_read_only(asset.snapshot_ref)
        except OSError as exc:
            raise LockError(f"read-only lock failed for {asset.asset_id!r}: {exc}") from exc
        locks.append(
            LockRecord(
                asset_id=asset.asset_id, final_ref=asset.snapshot_ref, sha256=asset.sha256
            )
        )
    return locks


def check_locks(workspace: CampaignWorkspace, locks: list[LockRecord]) -> list[str]:
    """→ asset_ids whose locked bytes no longer match their approval-chain hash."""
    violated: list[str] = []
    for lock in locks:
        try:
            content = workspace.download(lock.final_ref)
        except OSError:
            violated.append(lock.asset_id)  # missing/unreadable = integrity gone
            continue
        if sha256_hex(content) != lock.sha256:
            violated.append(lock.asset_id)
    return violated
