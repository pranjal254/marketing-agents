"""Retry with exponential backoff, timeouts, and the idempotency store.

Every external call in every agent goes through these primitives:
3 retries, exponential backoff from 2s, transient failures only (spec Error Handling).
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any


class TransientError(Exception):
    """An error worth retrying (HTTP 429/5xx, timeouts, connection resets)."""


class PermanentError(Exception):
    """An error that must not be retried (auth failure, 4xx, validation)."""


def with_retries[T](
    fn: Callable[[], T],
    *,
    retries: int = 3,
    base_delay_s: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (TransientError,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``; on a retryable failure wait base_delay * 2^attempt and retry.

    Raises the last error after ``retries`` failed retries.
    """
    attempt = 0
    while True:
        try:
            return fn()
        except retry_on:
            if attempt >= retries:
                raise
            sleep(base_delay_s * (2**attempt))
            attempt += 1


class IdempotencyStore:
    """Interface: check an idempotency key before any side effect (kit hard rule 9)."""

    def get(self, key: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def put(self, key: str, result: dict[str, Any]) -> None:
        raise NotImplementedError


class InMemoryIdempotencyStore(IdempotencyStore):
    def __init__(self) -> None:
        self._seen: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        return self._seen.get(key)

    def put(self, key: str, result: dict[str, Any]) -> None:
        self._seen.setdefault(key, result)


class SqliteIdempotencyStore(IdempotencyStore):
    """Durable idempotency store. Insert-only; a key is never overwritten.
    Thread-safe: single connection guarded by a lock (server threadpools)."""

    def __init__(self, path: str) -> None:
        import threading

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS idempotency"
                " (key TEXT PRIMARY KEY, result TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            self._conn.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        import json

        with self._lock:
            row = self._conn.execute(
                "SELECT result FROM idempotency WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return dict(json.loads(row[0]))

    def put(self, key: str, result: dict[str, Any]) -> None:
        import json
        from datetime import UTC, datetime

        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO idempotency (key, result, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(result), datetime.now(tz=UTC).isoformat()),
            )
            self._conn.commit()


def execute_idempotent(
    key: str,
    store: IdempotencyStore,
    side_effect: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """Run ``side_effect`` exactly once per key.

    Returns (result, was_repeat). On a repeat the prior result is returned unchanged
    and the side effect does not run.
    """
    prior = store.get(key)
    if prior is not None:
        return prior, True
    result = side_effect()
    store.put(key, result)
    return result, False
