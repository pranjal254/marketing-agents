"""Context Store backend selection + the Postgres contract.

Selection tests run everywhere. The Postgres contract tests need a real server
and are gated on TEST_DATABASE_URL (deliberately NOT DATABASE_URL, so pointing
tests at a production database requires an explicit act):

  set TEST_DATABASE_URL=postgresql://...  &&  pytest tests/test_postgres_store.py
"""

from __future__ import annotations

import os
import uuid

import pytest

from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import (
    SqliteContextStore,
    build_context_store,
    build_idempotency_store,
    store_backend,
)
from shiftai_shared.resilience import SqliteIdempotencyStore

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "")
needs_postgres = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_DATABASE_URL not set — Postgres contract tests skipped"
)


# ------------------------------------------------------------ backend selection


def test_default_backend_is_sqlite(tmp_path: object) -> None:
    settings = SharedSettings(_env_file=None)
    assert store_backend(settings) == "sqlite"
    store = build_context_store(settings, str(tmp_path) + "/s.sqlite")
    assert isinstance(store, SqliteContextStore)
    idem = build_idempotency_store(settings, str(tmp_path) + "/i.sqlite")
    assert isinstance(idem, SqliteIdempotencyStore)


def test_database_url_selects_postgres_backend() -> None:
    settings = SharedSettings(_env_file=None, DATABASE_URL="postgresql://x@localhost/db")
    assert store_backend(settings) == "postgres"
    # The URL is a secret: it must never appear in repr/logs.
    assert "postgresql://" not in repr(settings)


# ------------------------------------------------------- Postgres contract


@needs_postgres
def test_put_get_versions_query_contract() -> None:
    from shiftai_shared.context_store.migrate import run_migrations
    from shiftai_shared.context_store.postgres_store import PostgresContextStore

    run_migrations(TEST_DSN)
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    store = PostgresContextStore(TEST_DSN, tenant_id=tenant, client_id="tests")
    kind = "test_kind"
    first = store.put(kind, "k1", {"n": 1})
    second = store.put(kind, "k1", {"n": 2})
    assert (first.version, second.version) == (1, 2)
    latest = store.get(kind, "k1")
    assert latest is not None and latest.value == {"n": 2}
    assert [r.value["n"] for r in store.get_all_versions(kind, "k1")] == [1, 2]
    store.put(kind, "k2", {"n": 9})
    assert {r.key: r.value["n"] for r in store.query(kind)} == {"k1": 2, "k2": 9}
    assert store.get(kind, "missing") is None


@needs_postgres
def test_tenant_isolation_via_rls_scoping() -> None:
    from shiftai_shared.context_store.migrate import run_migrations
    from shiftai_shared.context_store.postgres_store import PostgresContextStore

    run_migrations(TEST_DSN)
    a = PostgresContextStore(TEST_DSN, tenant_id=f"a-{uuid.uuid4().hex[:8]}", client_id="tests")
    b = PostgresContextStore(TEST_DSN, tenant_id=f"b-{uuid.uuid4().hex[:8]}", client_id="tests")
    a.put("test_kind", "shared-key", {"who": "a"})
    assert b.get("test_kind", "shared-key") is None
    assert b.query("test_kind") == []


@needs_postgres
def test_idempotency_key_is_written_once() -> None:
    from shiftai_shared.context_store.migrate import run_migrations
    from shiftai_shared.context_store.postgres_store import PostgresIdempotencyStore

    run_migrations(TEST_DSN)
    store = PostgresIdempotencyStore(
        TEST_DSN, tenant_id=f"t-{uuid.uuid4().hex[:8]}", client_id="tests"
    )
    key = f"key-{uuid.uuid4().hex[:8]}"
    assert store.get(key) is None
    store.put(key, {"ref": "first"})
    store.put(key, {"ref": "second"})  # ignored: a key is never overwritten
    assert store.get(key) == {"ref": "first"}


@needs_postgres
def test_append_only_is_enforced_in_schema() -> None:
    import psycopg

    from shiftai_shared.context_store.migrate import run_migrations
    from shiftai_shared.context_store.postgres_store import PostgresContextStore

    run_migrations(TEST_DSN)
    tenant = f"t-{uuid.uuid4().hex[:8]}"
    store = PostgresContextStore(TEST_DSN, tenant_id=tenant, client_id="tests")
    store.put("test_kind", "immutable", {"n": 1})
    # autocommit so the first rejected statement doesn't abort the transaction
    # for the second assertion.
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute(
                "UPDATE context_records SET value = '{}'::jsonb WHERE tenant_id = %s",
                (tenant,),
            )
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM context_records WHERE tenant_id = %s", (tenant,))
