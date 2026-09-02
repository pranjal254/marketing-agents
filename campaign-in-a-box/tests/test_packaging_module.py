"""Deterministic packaging module: completeness diff, naming, snapshots — pure
functions, plus the no-LLM static guarantee."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from c2c_campaign_box.agent_config import OrchestratorConfig, load_orchestrator_config
from c2c_campaign_box.models import (
    AssetChecklistItem,
    ConfirmationRecord,
    RegisteredAsset,
)
from c2c_campaign_box.packaging.completeness import (
    completeness_diff,
    missing_confirmation_records,
)
from c2c_campaign_box.packaging.naming import flagged_issues, validate_names
from c2c_campaign_box.packaging.snapshot import (
    SnapshotReadError,
    plan_snapshots,
    verify_rehash,
)
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from tests.conftest import CONFIG_PATH

PACKAGING_DIR = Path(__file__).resolve().parents[1] / "src" / "c2c_campaign_box" / "packaging"


def _config() -> OrchestratorConfig:
    return load_orchestrator_config(CONFIG_PATH)


def _confirmation(version: int = 1) -> ConfirmationRecord:
    return ConfirmationRecord(
        kind="asset_content", decision="confirmed", actor_id="reviewer@x",
        actor_role="content-reviewer", timestamp="2026-09-02T10:00:00Z",
        deltas={"version": version},
    )


def _asset(asset_id: str, *, status: str = "content_confirmed", version: int = 1,
           confirmed: bool = True, filename: str | None = None) -> RegisteredAsset:
    return RegisteredAsset(
        asset_id=asset_id,
        asset_type=asset_id,
        filename=filename or f"erp-modernization-{asset_id.replace('_', '-')}-v{version}.docx",
        file_ref=f"/drafts/{asset_id}.docx",
        version=version,
        status=status,  # type: ignore[arg-type]
        confirmation=_confirmation(version) if confirmed else None,
    )


def _checklist(asset_ids: list[str]) -> list[AssetChecklistItem]:
    return [
        AssetChecklistItem(asset_id=a, asset_type=a, label=a, decision="create",
                           decision_rationale="x", status="content_confirmed")
        for a in asset_ids
    ]


# ------------------------------------------------------------- completeness


def test_empty_diff_when_all_confirmed() -> None:
    diff = completeness_diff(_checklist(["a", "b"]), [_asset("a"), _asset("b")])
    assert diff.empty


def test_missing_extra_and_unconfirmed_assets_block() -> None:
    diff = completeness_diff(
        _checklist(["a", "b"]),
        [_asset("a", status="in_production"), _asset("c")],
    )
    assert diff.missing == ["a", "b"]  # a not confirmed, b absent
    assert diff.extra == ["c"]
    assert not diff.empty


def test_version_mismatch_detected() -> None:
    asset = _asset("a", version=2)
    assert asset.confirmation is not None
    tampered = asset.model_copy(
        update={"confirmation": asset.confirmation.model_copy(update={"deltas": {"version": 1}})}
    )
    diff = completeness_diff(_checklist(["a"]), [tampered])
    assert diff.version_mismatch == ["a"]


def test_missing_confirmation_record_is_reported() -> None:
    assert missing_confirmation_records([_asset("a", confirmed=False)]) == ["a"]


# ------------------------------------------------------------------- naming


def test_exact_name_passes_and_case_variant_autocorrects() -> None:
    config = _config()
    ok = _asset("flagship_blog")
    variant = _asset("flagship_blog", filename="ERP-Modernization-Flagship-Blog-V1.docx")
    names, issues = validate_names(config, "erp-modernization", [ok, variant])
    assert names["flagship_blog"] == "erp-modernization-flagship-blog-v1.docx"
    assert len(issues) == 1 and issues[0].resolution == "auto_corrected"
    assert flagged_issues(issues) == []


def test_ambiguous_name_is_flagged_never_guessed() -> None:
    config = _config()
    odd = _asset("flagship_blog", filename="final-FINAL-draft(3).docx")
    _, issues = validate_names(config, "erp-modernization", [odd])
    assert [i.resolution for i in issues] == ["flagged"]


# ---------------------------------------------------------------- snapshots


def test_plan_snapshots_hashes_all_before_any_write(tmp_path: Path) -> None:
    ws = LocalCampaignWorkspace(str(tmp_path))
    ref = ws.upload("drafts", "a.docx", b"content-a")
    asset = _asset("a").model_copy(update={"file_ref": ref})
    plan = plan_snapshots(ws, [asset], {"a": "canonical-a.docx"})
    assert len(plan) == 1
    assert plan[0].sha256 == __import__("hashlib").sha256(b"content-a").hexdigest()
    assert verify_rehash(plan[0], b"content-a")
    assert not verify_rehash(plan[0], b"edited later")


def test_unreadable_asset_aborts_before_writes(tmp_path: Path) -> None:
    ws = LocalCampaignWorkspace(str(tmp_path))
    ghost = _asset("a").model_copy(update={"file_ref": str(tmp_path / "missing.docx")})
    with pytest.raises(SnapshotReadError):
        plan_snapshots(ws, [ghost], {"a": "x.docx"})


# ------------------------------------------------------------ no-LLM static


def test_packaging_module_never_imports_llm_or_provider() -> None:
    """Spec: the packaging module is deterministic — no LLM. Enforced statically."""
    banned = ("llm", "provider", "anthropic", "openai", "planning")
    for path in PACKAGING_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(b in name.lower() for b in banned), (
                    f"{path.name} imports {name!r} — packaging must stay LLM-free"
                )
