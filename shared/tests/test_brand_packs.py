"""Multi-brand-pack governance: every committed pack must load and validate, the
active pack is an explicit selection (unknown names fail loudly, never fall
back), and the prompt block carries the right brand identity."""

from __future__ import annotations

import pytest

from shiftai_shared.brand import (
    BRAND_PACKS,
    brand_prompt_block,
    lint_text,
    load_brand_rules,
)


def test_every_committed_pack_loads_and_validates() -> None:
    for pack in BRAND_PACKS:
        rules = load_brand_rules(pack)
        assert rules.positioning and rules.tone
        assert rules.voice and rules.personas
        assert {"blog", "email"} <= set(rules.playbooks)


def test_unknown_pack_fails_loudly() -> None:
    with pytest.raises(ValueError, match="unknown brand pack"):
        load_brand_rules("nonexistent")


def test_default_pack_is_levelshift() -> None:
    rules = load_brand_rules()
    assert rules.brand_name == "LevelShift"
    assert brand_prompt_block(rules).startswith("LevelShift Brand Rules")


def test_demandblue_pack_identity_and_lint() -> None:
    rules = load_brand_rules("demandblue")
    assert rules.brand_name == "DemandBlue"
    block = brand_prompt_block(rules)
    assert block.startswith("DemandBlue Brand Rules")
    assert "on-demand" in rules.positioning
    # Banned hype terms and urgency framing are errors under the DemandBlue pack.
    findings = lint_text("A world-class, revolutionary offer — act now!", rules)
    flagged = {(f.rule_id, f.term) for f in findings}
    assert ("banned_term", "world-class") in flagged
    assert ("banned_term", "revolutionary") in flagged
    assert ("urgency_fear", "act now") in flagged
    # Partner-safe, sourced copy passes clean.
    clean = "DemandBlue helps teams get more from Sales Cloud with on-demand experts."
    assert lint_text(clean, rules) == []
