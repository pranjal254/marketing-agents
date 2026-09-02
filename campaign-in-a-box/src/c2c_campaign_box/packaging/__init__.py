"""Deterministic packaging module (spec steps 9-12) — NO LLM anywhere in this
subpackage (enforced by a static test). Pure functions over checklist +
confirmations + rules: completeness diff, naming enforcement, hashed snapshots,
transactional manifest assembly.
"""

from c2c_campaign_box.packaging.completeness import completeness_diff
from c2c_campaign_box.packaging.naming import validate_names
from c2c_campaign_box.packaging.snapshot import SnapshotPlanItem, plan_snapshots

__all__ = ["SnapshotPlanItem", "completeness_diff", "plan_snapshots", "validate_names"]
