"""Versioned migration runner for the Postgres Context Store.

  python -m shiftai_shared.context_store.migrate

Applies every ``migrations/NNNN_*.sql`` in order, once, recording each in
``schema_migrations``. Idempotent: re-running applies only what is new. Runs as
the database owner (migrations are the only mutation surface in the system —
the runtime roles cannot alter schema or data history).
"""

from __future__ import annotations

from importlib import resources
from typing import Any

from shiftai_shared.config import load_settings


def pending_migrations(applied: set[str]) -> list[tuple[str, str]]:
    """(name, sql) for every packaged migration not yet applied, in order."""
    root = resources.files("shiftai_shared").joinpath("context_store/migrations")
    out: list[tuple[str, str]] = []
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".sql") and entry.name not in applied:
            out.append((entry.name, entry.read_text(encoding="utf-8")))
    return out


def run_migrations(dsn: str) -> list[str]:
    """Apply pending migrations; returns the names applied this run."""
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "psycopg is not installed — pip install 'shiftai-shared[postgres]'"
        ) from exc
    applied_now: list[str] = []
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        rows: list[tuple[Any, ...]] = conn.execute(
            "SELECT name FROM schema_migrations"
        ).fetchall()
        applied = {str(r[0]) for r in rows}
        for name, sql in pending_migrations(applied):
            conn.execute(sql)  # each file manages its own BEGIN/COMMIT
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            conn.commit()
            applied_now.append(name)
    return applied_now


def main() -> int:
    settings = load_settings()
    if settings.database_url is None:
        print("DATABASE_URL is not set — nothing to migrate (SQLite mode).")
        return 2
    applied = run_migrations(settings.database_url.get_secret_value())
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
