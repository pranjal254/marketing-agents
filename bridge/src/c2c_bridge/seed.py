"""Session workspace bootstrap (bridge only) — NO canned marketing content.

The reusable-asset repository starts EMPTY: reuse/adapt decisions arise only
from assets users actually provide, never from synthetic samples. The intel
library is GENERATED at session start from the committed brand rules pack:
marketing-approved practice positioning and proof points (straight from the
Brand Playbook). That gives planning real, sourced claim material with zero
dependence on local knowledge folders — deployments have nothing to mount."""

from __future__ import annotations

from pathlib import Path

from shiftai_shared.brand import BrandRules


def seed_dev_workspace(box_workspace: Path, repository: Path, rules: BrandRules) -> None:
    repository.mkdir(parents=True, exist_ok=True)  # empty on purpose
    intel_dir = box_workspace / "02-Reference" / "intel-library"
    intel_dir.mkdir(parents=True, exist_ok=True)
    # One file per practice so each surfaces as its own intel signal (excerpted);
    # the proof point leads so it always lands inside the excerpt window.
    for practice in rules.practices:
        target = intel_dir / f"{practice.id}-practice.md"
        if target.exists():
            continue
        target.write_text(
            f"# {practice.name} ({rules.brand_name} Brand Playbook, "
            f"pack {rules.rules_pack_id} v{rules.version})\n\n"
            f"Approved proof point: {practice.proof_point}\n\n"
            f"Tagline: {practice.tagline}\n"
            f"Offer: {practice.offer}\n"
            f"Pain solved: {practice.pain}\n"
            f"Benefit: {practice.benefit}\n",
            encoding="utf-8",
        )
    credentials = intel_dir / "credentials.md"
    if not credentials.exists() and rules.credentials:
        credentials.write_text(
            f"# {rules.brand_name} credentials (Brand Playbook, "
            f"pack {rules.rules_pack_id} v{rules.version})\n\n"
            + "\n".join(f"- {c}" for c in rules.credentials)
            + "\n",
            encoding="utf-8",
        )
