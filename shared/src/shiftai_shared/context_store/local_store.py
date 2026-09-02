"""Local Context Store implementations: in-memory (tests) and SQLite (dev runs).

Insert-only by construction — the SQL surface contains no UPDATE and no DELETE.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from shiftai_shared.context_store.store import StoredRecord


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class InMemoryContextStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], list[StoredRecord]] = {}
        self._lock = threading.Lock()

    def put(self, kind: str, key: str, value: dict[str, Any]) -> StoredRecord:
        with self._lock:
            versions = self._records.setdefault((kind, key), [])
            record = StoredRecord(
                kind=kind,
                key=key,
                version=len(versions) + 1,
                value=json.loads(json.dumps(value, default=str)),
                created_at=_now(),
            )
            versions.append(record)
            return record

    def get(self, kind: str, key: str) -> StoredRecord | None:
        versions = self._records.get((kind, key), [])
        return versions[-1] if versions else None

    def get_all_versions(self, kind: str, key: str) -> list[StoredRecord]:
        return list(self._records.get((kind, key), []))

    def query(self, kind: str) -> list[StoredRecord]:
        out: list[StoredRecord] = []
        for (k, _), versions in self._records.items():
            if k == kind and versions:
                out.append(versions[-1])
        return out


class SqliteContextStore:
    def __init__(self, path: str) -> None:
        # check_same_thread=False + the internal lock: safe under server threadpools
        # (FastAPI/uvicorn run sync endpoints on worker threads).
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS records ("
            " kind TEXT NOT NULL, key TEXT NOT NULL, version INTEGER NOT NULL,"
            " value TEXT NOT NULL, created_at TEXT NOT NULL,"
            " PRIMARY KEY (kind, key, version))"
        )
        self._conn.commit()

    def put(self, kind: str, key: str, value: dict[str, Any]) -> StoredRecord:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM records WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
            version = int(row[0]) + 1
            created = _now()
            self._conn.execute(
                "INSERT INTO records (kind, key, version, value, created_at) VALUES (?,?,?,?,?)",
                (kind, key, version, json.dumps(value, default=str), created),
            )
            self._conn.commit()
            return StoredRecord(
                kind=kind, key=key, version=version, value=value, created_at=created
            )

    def get(self, kind: str, key: str) -> StoredRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, value, created_at FROM records"
                " WHERE kind = ? AND key = ? ORDER BY version DESC LIMIT 1",
                (kind, key),
            ).fetchone()
        if row is None:
            return None
        return StoredRecord(kind, key, int(row[0]), json.loads(row[1]), str(row[2]))

    def get_all_versions(self, kind: str, key: str) -> list[StoredRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, value, created_at FROM records"
                " WHERE kind = ? AND key = ? ORDER BY version",
                (kind, key),
            ).fetchall()
        return [StoredRecord(kind, key, int(v), json.loads(val), str(c)) for v, val, c in rows]

    def query(self, kind: str) -> list[StoredRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT r.key, r.version, r.value, r.created_at FROM records r"
                " JOIN (SELECT key, MAX(version) mv FROM records WHERE kind = ? GROUP BY key)"
                " latest ON r.key = latest.key AND r.version = latest.mv WHERE r.kind = ?",
                (kind, kind),
            ).fetchall()
        return [
            StoredRecord(kind, str(k), int(v), json.loads(val), str(c)) for k, v, val, c in rows
        ]
