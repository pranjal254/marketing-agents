"""Postgres bindings for the Context Store and the idempotency ledger.

Implements the exact same protocols as the local SQLite bindings — agents are
unaware of the backend. Enterprise properties live in the schema (see
``migrations/``): versioned append-only tables, row-level security on the
``app.tenant_id`` connection setting, least-privilege roles, mutation-blocking
triggers. This module contains INSERT and SELECT statements only.

psycopg (v3) is an optional dependency: ``pip install shiftai-shared[postgres]``.
The connection string comes from settings (``DATABASE_URL``) — env-only, never
code; hosted databases should require TLS (``sslmode=require``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from typing import TYPE_CHECKING, Any

from shiftai_shared.context_store.store import StoredRecord
from shiftai_shared.resilience import IdempotencyStore

if TYPE_CHECKING:
    from psycopg import Connection


class PostgresUnavailableError(RuntimeError):
    """DATABASE_URL is set but the optional postgres extra is not installed."""


def _connect(dsn: str, tenant_id: str, client_id: str) -> Connection[Any]:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise PostgresUnavailableError(
            "DATABASE_URL is set but psycopg is not installed — "
            "install the optional extra: pip install 'shiftai-shared[postgres]'"
        ) from exc
    conn = psycopg.connect(dsn, autocommit=True)
    # Session identity: row-level security filters on app.tenant_id; created_by
    # defaults from app.agent_id. Both are set once per connection, never per row.
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id,))
    conn.execute("SELECT set_config('app.agent_id', %s, false)", (client_id,))
    return conn


def _canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class _PgBase:
    """One guarded connection with a single reconnect retry (server restarts,
    pooler idle timeouts). Safe under server threadpools, like the SQLite bindings."""

    def __init__(self, dsn: str, tenant_id: str, client_id: str) -> None:
        self._dsn = dsn
        self._tenant = tenant_id
        self._client = client_id
        self._lock = threading.Lock()
        self._conn = _connect(dsn, tenant_id, client_id)

    def _execute(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        import psycopg

        with self._lock:
            for attempt in (1, 2):
                try:
                    cur = self._conn.execute(sql, params)
                    return cur.fetchall() if cur.description is not None else []
                except psycopg.OperationalError:
                    if attempt == 2:
                        raise
                    with contextlib.suppress(Exception):  # connection already broken
                        self._conn.close()
                    self._conn = _connect(self._dsn, self._tenant, self._client)
        return []  # unreachable; keeps type checkers satisfied


class PostgresContextStore(_PgBase):
    """Versioned append-only store over ``context_records`` (see 0001_engine.sql)."""

    def put(self, kind: str, key: str, value: dict[str, Any]) -> StoredRecord:
        import psycopg

        canonical = _canonical(value)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        # Version assignment races across processes resolve via the primary key:
        # on a conflict the losing writer recomputes MAX(version)+1 and retries.
        for attempt in range(1, 4):
            try:
                rows = self._execute(
                    "INSERT INTO context_records"
                    " (tenant_id, kind, key, version, value, value_sha256)"
                    " SELECT %s, %s, %s, COALESCE(MAX(version), 0) + 1, %s::jsonb, %s"
                    " FROM context_records WHERE tenant_id = %s AND kind = %s AND key = %s"
                    " RETURNING version, created_at",
                    (self._tenant, kind, key, canonical, digest, self._tenant, kind, key),
                )
                return StoredRecord(
                    kind=kind, key=key, version=int(rows[0][0]),
                    value=json.loads(canonical), created_at=str(rows[0][1]),
                )
            except psycopg.errors.UniqueViolation:
                if attempt == 3:
                    raise
        raise RuntimeError("unreachable")  # pragma: no cover

    def get(self, kind: str, key: str) -> StoredRecord | None:
        rows = self._execute(
            "SELECT version, value, created_at FROM context_records"
            " WHERE tenant_id = %s AND kind = %s AND key = %s"
            " ORDER BY version DESC LIMIT 1",
            (self._tenant, kind, key),
        )
        if not rows:
            return None
        version, value, created = rows[0]
        return StoredRecord(kind, key, int(version), dict(value), str(created))

    def get_all_versions(self, kind: str, key: str) -> list[StoredRecord]:
        rows = self._execute(
            "SELECT version, value, created_at FROM context_records"
            " WHERE tenant_id = %s AND kind = %s AND key = %s ORDER BY version",
            (self._tenant, kind, key),
        )
        return [StoredRecord(kind, key, int(v), dict(val), str(c)) for v, val, c in rows]

    def query(self, kind: str) -> list[StoredRecord]:
        rows = self._execute(
            "SELECT r.key, r.version, r.value, r.created_at FROM context_records r"
            " JOIN (SELECT key, MAX(version) AS mv FROM context_records"
            "       WHERE tenant_id = %s AND kind = %s GROUP BY key) latest"
            " ON r.key = latest.key AND r.version = latest.mv"
            " WHERE r.tenant_id = %s AND r.kind = %s",
            (self._tenant, kind, self._tenant, kind),
        )
        return [
            StoredRecord(kind, str(k), int(v), dict(val), str(c)) for k, v, val, c in rows
        ]


class PostgresIdempotencyStore(_PgBase, IdempotencyStore):
    """Durable side-effect ledger over ``idempotency_keys``. Insert-only —
    a key is never overwritten (ON CONFLICT DO NOTHING mirrors the SQLite binding)."""

    def get(self, key: str) -> dict[str, Any] | None:
        rows = self._execute(
            "SELECT result FROM idempotency_keys WHERE tenant_id = %s AND key = %s",
            (self._tenant, key),
        )
        return dict(rows[0][0]) if rows else None

    def put(self, key: str, result: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO idempotency_keys (tenant_id, key, result)"
            " VALUES (%s, %s, %s::jsonb) ON CONFLICT (tenant_id, key) DO NOTHING",
            (self._tenant, key, json.dumps(result, default=str)),
        )
