from __future__ import annotations

import json
from pathlib import Path

import pydantic
import pytest

from shiftai_shared.business_capability import load_decision_config
from shiftai_shared.context_store import InMemoryContextStore, SqliteContextStore

CONFIG_JSON = {
    "agentType": "decision",
    "agentId": "test_agent",
    "version": "0.1.0",
    "intakeSchema": [{"field": "f1", "type": "text", "required": True}],
    "policyRules": [{"id": "r1", "condition": "missing(f1)", "resultActionClass": "class_a"}],
    "actionClassTaxonomy": [{"id": "class_a", "label": "A", "description": "does A"}],
    "authorityEnvelope": {
        "impactCeiling": {"tier1": "none"},
        "reversibilityRules": {"always": "reversible"},
        "domainBoundary": "test boundary",
        "dataRecencyMaxDays": 14,
    },
    "routingMap": [{"uncertaintyType": "confidence_only", "routesTo": "queue-1"}],
    "tierThresholds": {"tier1": "a", "tier2": "b", "tier3": "c"},
    "reasonCodes": ["code_1"],
    "precedentDecayDays": 90,
    "reasoningProvider": "claude",
}


def test_config_loads_and_is_frozen(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG_JSON), encoding="utf-8")
    config = load_decision_config(path)
    assert config.agent_id == "test_agent"
    assert config.route_for("confidence_only") == "queue-1"
    assert config.action_class_ids() == ["class_a"]
    with pytest.raises(pydantic.ValidationError):
        config.version = "9.9.9"  # type: ignore[misc]


def test_config_module_has_no_write_surface() -> None:
    import shiftai_shared.business_capability as bc

    public = {n for n in dir(bc) if not n.startswith("_") and callable(getattr(bc, n))}
    assert not any(n.startswith(("save", "write", "update", "delete")) for n in public)


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_store_versions_append_only(store_kind: str, tmp_path: Path) -> None:
    store = (
        InMemoryContextStore()
        if store_kind == "memory"
        else SqliteContextStore(str(tmp_path / "cs.sqlite"))
    )
    store.put("case_log", "case-1", {"status": "draft"})
    store.put("case_log", "case-1", {"status": "awaiting_input"})
    latest = store.get("case_log", "case-1")
    assert latest is not None and latest.version == 2
    assert latest.value["status"] == "awaiting_input"
    versions = store.get_all_versions("case_log", "case-1")
    assert [v.version for v in versions] == [1, 2]
    assert versions[0].value["status"] == "draft"  # old versions immutable

    exposed = {n for n in dir(type(store)) if not n.startswith("_")}
    assert not any(("delete" in n or "update" in n or "remove" in n) for n in exposed)


def test_store_query_latest_per_key(tmp_path: Path) -> None:
    store = SqliteContextStore(str(tmp_path / "cs.sqlite"))
    store.put("calendar", "c1", {"topic": "t1"})
    store.put("calendar", "c2", {"topic": "t2"})
    store.put("calendar", "c1", {"topic": "t1-updated"})
    latest = {r.key: r.value["topic"] for r in store.query("calendar")}
    assert latest == {"c1": "t1-updated", "c2": "t2"}
