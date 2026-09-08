"""DemandBlue wiring: external marketing-folder binding, BU config selection via
env, and cross-config coverage — mock provider, temp dirs, no network.

The binding contract under test: C2C_MARKETING_ROOT points the bridge at the
marketing team's real folder tree (campaign/, repository/, references/
intel-library/) and the synthetic dev seed is then skipped; C2C_BRAND_PACK and
C2C_*_CONFIG select the business-unit brand pack and versioned agent configs
without touching agent code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import AGENTS_ROOT, create_app, resolve_binding_paths

DB_BOX_CONFIG = AGENTS_ROOT / "campaign-in-a-box" / "config" / "campaign_in_a_box.demandblue.json"
DB_REPURPOSE_CONFIG = (
    AGENTS_ROOT / "content-repurposing" / "config" / "content_repurposing.demandblue.json"
)


def _marketing_tree(tmp_path: Path) -> Path:
    root = tmp_path / "shiftai-marketing"
    for sub in ("intake", "campaign", "references/intel-library", "repository"):
        (root / sub).mkdir(parents=True)
    (root / "references" / "intel-library" / "ecosystem-notes.md").write_text(
        "# Curated intel\nSalesforce customers under-use Einstein features.", encoding="utf-8"
    )
    return root


# ------------------------------------------------------------ path resolution


def test_resolve_binding_paths_default_is_sandboxed(tmp_path: Path) -> None:
    paths = resolve_binding_paths(tmp_path, marketing_root="")
    assert paths.marketing_root is None
    assert paths.box_workspace == tmp_path / "box-workspace"
    assert paths.repository == tmp_path / "repository"
    assert not Path(paths.intel_library).is_absolute()  # workspace-relative default


def test_resolve_binding_paths_external(tmp_path: Path) -> None:
    root = _marketing_tree(tmp_path)
    paths = resolve_binding_paths(tmp_path, marketing_root=str(root))
    assert paths.marketing_root is not None
    assert paths.box_workspace == paths.marketing_root / "campaign"
    assert paths.repository == paths.marketing_root / "repository"
    assert Path(paths.intel_library) == paths.marketing_root / "references" / "intel-library"


def test_resolve_binding_paths_missing_root_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_binding_paths(tmp_path, marketing_root=str(tmp_path / "does-not-exist"))


# ------------------------------------------------------- full bridge binding


@pytest.fixture()
def db_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, Path]:
    from shiftai_shared.llm import MockLLMProvider

    import c2c_bridge.app as app_mod

    monkeypatch.setattr(app_mod, "build_provider", lambda _s: MockLLMProvider(default="{}"))
    root = _marketing_tree(tmp_path)
    monkeypatch.setenv("C2C_MARKETING_ROOT", str(root))
    monkeypatch.setenv("C2C_BRAND_PACK", "demandblue")
    monkeypatch.setenv("C2C_BOX_CONFIG", str(DB_BOX_CONFIG))
    monkeypatch.setenv("C2C_REPURPOSE_CONFIG", str(DB_REPURPOSE_CONFIG))
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app), root


def test_external_binding_reports_and_never_seeds(db_bridge: tuple[TestClient, Path]) -> None:
    client, root = db_bridge
    health = client.get("/api/health").json()
    assert health["brand_pack"] == "demandblue"
    assert health["marketing_root"] is not None
    assert Path(health["marketing_root"]).name == root.name
    # The real marketing folder never receives synthetic dev-seed files.
    assert list((root / "repository").rglob("*")) == []
    assert not (root / "campaign" / "02-Reference").exists()


def test_demandblue_configs_selected_and_consistent(
    db_bridge: tuple[TestClient, Path],
) -> None:
    client, _ = db_bridge
    meta = client.get("/api/meta").json()
    composition = {c["asset_type"]: c for c in meta["box"]["composition"]}
    # Real-plan volumes: the DemandBlue nurture runs up to 11 email touchpoints.
    assert composition["email_touchpoints"]["volume_cap"] == 11
    assert "case_study" in composition
    assert composition["call_scripts"]["label"].startswith("ISR")
    # Every derivative asset type Agent 2 can plan has an Agent 3 recipe.
    recipes = {r["asset_type"] for r in meta["repurposing"]["recipes"]}
    derivative = {t for t, c in composition.items() if c["review_gate"] == "derivative"}
    assert derivative <= recipes
    assert meta["repurposing"]["config_version"] == "0.1.0"
    # The flagship contract holds across Agents 2 and 3.
    flagships = [t for t, c in composition.items() if c["review_gate"] == "flagship"]
    assert flagships == ["flagship_blog"]
