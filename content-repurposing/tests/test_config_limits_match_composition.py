"""The variant ceiling and the planned volumes live in two different config
packs, one per agent. If the ceiling drops below what the composition already
plans, every campaign silently produces fewer assets than the business unit
asked for, with nothing in the UI to say so.

These tests pair the packs up so that cannot happen quietly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from c2c_content_repurposing.agent_config import load_repurposing_config

AGENTS_ROOT = Path(__file__).resolve().parents[2]
BOX_CONFIG_DIR = AGENTS_ROOT / "campaign-in-a-box" / "config"
REPURPOSE_CONFIG_DIR = AGENTS_ROOT / "content-repurposing" / "config"

# Same business unit, one pack per agent. Keyed by the suffix the deployment
# picks with C2C_BOX_CONFIG / C2C_REPURPOSE_CONFIG.
PACK_PAIRS = [
    ("levelshift", "campaign_in_a_box.json", "content_repurposing.json"),
    (
        "demandblue",
        "campaign_in_a_box.demandblue.json",
        "content_repurposing.demandblue.json",
    ),
]


@pytest.mark.parametrize(("unit", "box_file", "repurpose_file"), PACK_PAIRS)
def test_ceiling_is_not_below_any_planned_volume(
    unit: str, box_file: str, repurpose_file: str
) -> None:
    composition = json.loads(
        (BOX_CONFIG_DIR / box_file).read_text(encoding="utf-8")
    )["composition"]
    config = load_repurposing_config(REPURPOSE_CONFIG_DIR / repurpose_file)
    ceiling = config.content_limits.max_variants_per_asset

    over = {
        item["assetType"]: item["volumeCap"]
        for item in composition
        if item["volumeCap"] > ceiling
    }
    assert not over, (
        f"{unit}: these asset types plan more variants than the configured ceiling "
        f"of {ceiling}, so every campaign would quietly produce fewer than the plan "
        f"asks for: {over}"
    )


@pytest.mark.parametrize(("unit", "box_file", "repurpose_file"), PACK_PAIRS)
def test_every_planned_asset_type_has_a_word_range_or_a_usable_default(
    unit: str, box_file: str, repurpose_file: str
) -> None:
    composition = json.loads(
        (BOX_CONFIG_DIR / box_file).read_text(encoding="utf-8")
    )["composition"]
    config = load_repurposing_config(REPURPOSE_CONFIG_DIR / repurpose_file)
    limits = config.content_limits

    for item in composition:
        asset_type = item["assetType"]
        word_range = limits.word_range_for(asset_type)
        assert limits.word_floor <= word_range.min_words <= word_range.max_words
        assert word_range.max_words <= limits.word_ceiling, (
            f"{unit}/{asset_type}: configured range exceeds the word ceiling"
        )
