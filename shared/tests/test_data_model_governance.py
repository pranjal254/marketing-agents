"""Data-model governance, enforced in CI (docs/data-model.md §4):

- every record kind an agent writes is registered in the governance catalog
  (0002) with classification/personal-data/retention metadata — a KIND_* constant
  without a catalog row fails the build (registration by migration, not runtime FK,
  so dev SQLite and prod Postgres behave identically);
- the engine migration (0001) stays domain-free, mirroring shared/ layering;
- append-only is enforced in-schema: RLS + mutation-blocking triggers on every
  table, and no runtime role holds UPDATE/DELETE/TRUNCATE;
- the adapter's SQL surface contains INSERT and SELECT only.
"""

from __future__ import annotations

import re
from pathlib import Path

AGENTS_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = (
    AGENTS_ROOT / "shared" / "src" / "shiftai_shared" / "context_store" / "migrations"
)
ENGINE_SQL = (MIGRATIONS / "0001_engine.sql").read_text(encoding="utf-8")
CAPABILITY_SQL = (MIGRATIONS / "0002_capability_c2c.sql").read_text(encoding="utf-8")

AGENT_SOURCES = [
    AGENTS_ROOT / "campaign-identification" / "src" / "campaign_identification",
    AGENTS_ROOT / "campaign-in-a-box" / "src" / "c2c_campaign_box",
    AGENTS_ROOT / "content-repurposing" / "src" / "c2c_content_repurposing",
]

KIND_CONSTANT = re.compile(r'^KIND_[A-Z_]+\s*=\s*"([a-z_]+)"', re.MULTILINE)


def _kinds_in_code() -> set[str]:
    kinds: set[str] = set()
    for src in AGENT_SOURCES:
        assert src.is_dir(), f"agent source tree moved: {src}"
        for path in src.rglob("*.py"):
            kinds |= set(KIND_CONSTANT.findall(path.read_text(encoding="utf-8")))
    return kinds


def _kinds_in_catalog() -> set[str]:
    seed = CAPABILITY_SQL.split("INSERT INTO record_kinds", 1)[1].split("ON CONFLICT", 1)[0]
    return set(re.findall(r"^\s*\('([a-z_]+)',", seed, re.MULTILINE))


def test_every_kind_constant_is_registered_in_the_catalog() -> None:
    missing = _kinds_in_code() - _kinds_in_catalog()
    assert not missing, (
        f"record kinds written by agents but missing from the governance catalog "
        f"(add rows to a new context_store migration): {sorted(missing)}"
    )


def test_views_reference_only_registered_kinds() -> None:
    referenced = set(re.findall(r"kind\s*=\s*'([a-z_]+)'", CAPABILITY_SQL))
    for group in re.findall(r"kind\s+IN\s*\(([^)]+)\)", CAPABILITY_SQL):
        referenced |= set(re.findall(r"'([a-z_]+)'", group))
    unknown = referenced - _kinds_in_catalog()
    assert not unknown, f"views reference unregistered kinds: {sorted(unknown)}"


def test_engine_migration_is_domain_free() -> None:
    # Same vocabulary rule as test_plane_isolation: engine mechanics carry no
    # Content-to-Campaign domain terms; capability content (0002) is exempt.
    lower = ENGINE_SQL.lower()
    for term in ["campaign", "brief", "vertical", "flagship", "semrush", "pardot"]:
        assert term not in lower, f"domain term {term!r} leaked into the engine migration"


def test_engine_tables_have_rls_and_append_only_triggers() -> None:
    normalized = re.sub(r"\s+", " ", ENGINE_SQL)
    for table in ("context_records", "idempotency_keys", "telemetry_events"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in normalized, (
            f"RLS not enabled on {table}"
        )
        # FORCE: tenancy must hold even for the table owner (single-user DSNs).
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in normalized, (
            f"RLS not forced on {table}"
        )
        assert f"CREATE POLICY tenant_isolation ON {table}" in ENGINE_SQL
        assert f"{table}_append_only" in ENGINE_SQL, f"no mutation trigger on {table}"


def test_runtime_roles_cannot_mutate_or_delete() -> None:
    grants = [line for line in ENGINE_SQL.splitlines() if line.strip().startswith("GRANT")]
    for line in grants:
        assert "UPDATE" not in line and "DELETE" not in line and "TRUNCATE" not in line, (
            f"mutating grant to a runtime role: {line.strip()}"
        )
    assert "REVOKE UPDATE, DELETE, TRUNCATE" in ENGINE_SQL


def test_adapter_sql_surface_is_insert_and_select_only() -> None:
    adapter = (
        AGENTS_ROOT / "shared" / "src" / "shiftai_shared" / "context_store"
        / "postgres_store.py"
    ).read_text(encoding="utf-8")
    sql_strings = re.findall(r'"([^"]*)"', adapter)
    for fragment in sql_strings:
        upper = fragment.upper()
        assert "UPDATE " not in upper or "ON CONFLICT" in upper, (
            f"UPDATE in adapter SQL: {fragment!r}"
        )
        assert "DELETE " not in upper, f"DELETE in adapter SQL: {fragment!r}"
        assert "DROP " not in upper, f"DROP in adapter SQL: {fragment!r}"


def test_catalog_metadata_columns_exist_for_governance() -> None:
    for column in ("data_classification", "contains_personal_data", "retention_days"):
        assert column in ENGINE_SQL, f"governance column {column!r} missing from catalog"
