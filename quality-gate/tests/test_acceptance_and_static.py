"""Acceptance criteria + static guardrails, enforced in CI:

1. The gate flags and blocks — it never edits: the only workspace write surface
   in this package is the final/ lock copy (locking.py).
2. Approvals are human-only: no code path in this package invokes
   record_review_outcome / record_false_negative.
3. Verdicts are a pure function of blocking findings + check completeness.
4. All rules cite versioned config: every reason code routes somewhere; every
   composition asset type (default AND DemandBlue) has a distribution class and
   a review sequence.
5. No Salesforce/Pardot connector code anywhere in the package.
"""

from __future__ import annotations

import re
from pathlib import Path

from c2c_campaign_box.agent_config import load_orchestrator_config

from c2c_quality_gate.agent_config import load_quality_gate_config
from c2c_quality_gate.grounding import verdict_for
from c2c_quality_gate.models import CONTEXTUAL_RULES, Finding

AGENT_ROOT = Path(__file__).resolve().parents[1]
SRC = AGENT_ROOT / "src" / "c2c_quality_gate"
AGENTS_ROOT = AGENT_ROOT.parent
CONFIG = load_quality_gate_config(AGENT_ROOT / "config" / "quality_gate.json")


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(SRC.rglob("*.py"))}


def test_no_salesforce_or_pardot_anywhere() -> None:
    for name, text in _sources().items():
        lower = text.lower()
        assert "salesforce" not in lower, name
        assert "pardot" not in lower, name


def test_no_code_path_approves_anything() -> None:
    """Approvals and calibration entries exist only as identity-stamped human
    actions carried in by the bridge/Execution Studio."""
    pattern = re.compile(r"\.(record_review_outcome|record_false_negative)\(")
    for name, text in _sources().items():
        assert not pattern.search(text), f"agent code invokes a human gate in {name}"


def test_gate_never_writes_to_the_workspace() -> None:
    """The gate flags, blocks, routes and locks IN PLACE — packaging already put
    the snapshot in final/, so this package has zero workspace write surface."""
    for name, text in _sources().items():
        assert ".upload(" not in text, f"unexpected workspace write in {name}"


def test_verdict_is_a_pure_function_of_blocking_findings() -> None:
    advisory = Finding(rule_id="avoid_term", severity="advisory", source="deterministic")
    blocking = Finding(rule_id="banned_term", severity="blocking", source="deterministic")
    assert verdict_for([], True) == "pass"
    assert verdict_for([advisory], True) == "pass"
    assert verdict_for([advisory, blocking], True) == "fail"
    assert verdict_for([], False) == "fail"  # fail-closed on incomplete checks


def test_every_reason_code_routes_somewhere() -> None:
    for reason in CONFIG.reason_codes:
        assert CONFIG.route_for(reason)


def test_contextual_rules_are_the_specs_five() -> None:
    assert set(CONTEXTUAL_RULES) == {
        "bc_fo_meaning", "copilot_scope", "claim_sourcing",
        "tone_urgency_fear", "brand_voice",
    }
    assert CONTEXTUAL_RULES["brand_voice"] == "advisory"  # style stays advisory


def test_routing_policy_covers_every_composition_asset_type() -> None:
    for config_name in ("campaign_in_a_box.json", "campaign_in_a_box.demandblue.json"):
        box = load_orchestrator_config(
            AGENTS_ROOT / "campaign-in-a-box" / "config" / config_name
        )
        for item in box.composition:
            cls = CONFIG.class_for(item.asset_type)
            assert item.asset_type in CONFIG.distribution_classes, (
                f"{item.asset_type} missing from routing policy ({config_name})"
            )
            assert CONFIG.sequence_for(cls), f"empty review sequence for {cls}"


def test_internal_assets_get_the_lighter_pass_public_the_full_one() -> None:
    assert CONFIG.class_for("battle_card") == "internal"
    assert CONFIG.class_for("flagship_blog") == "public"
    assert CONFIG.class_for("never_seen_type") == "public"  # unknown → deeper path
    public_steps = [s.step for s in CONFIG.sequence_for("public")]
    internal_steps = [s.step for s in CONFIG.sequence_for("internal")]
    assert public_steps == ["grammar_qa"]
    assert internal_steps == ["grammar_qa_light"]
    assert CONFIG.package_signoff.role == "bu-campaign-lead"


def test_system_prompt_is_spec_verbatim_and_versioned() -> None:
    from c2c_quality_gate import SYSTEM_PROMPT_VERSION
    from c2c_quality_gate.generation import load_system_prompt

    prompt = load_system_prompt()
    assert SYSTEM_PROMPT_VERSION == "1.0.0"
    assert prompt.startswith("You are the contextual compliance checker")
    assert "never rewrite the content yourself" in prompt
    assert "never default a rule to pass" in prompt


def test_gate_kinds_are_registered_in_the_governance_catalog() -> None:
    from c2c_quality_gate import persistence as db

    migration = (
        AGENTS_ROOT / "shared" / "src" / "shiftai_shared" / "context_store"
        / "migrations" / "0004_capability_c2c_agent5.sql"
    ).read_text(encoding="utf-8")
    for kind in (db.KIND_ASSET_REPORT, db.KIND_PACKAGE_GATE, db.KIND_REVIEW_TASK,
                 db.KIND_APPROVAL, db.KIND_CALIBRATION):
        assert f"('{kind}'" in migration, f"{kind} missing from migration 0004"
