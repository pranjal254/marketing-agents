"""The workspace user directory: identity, role and status for the people who use
the studio. Backed by a mutable Postgres table in a real deployment (persists
across browsers and restarts), or an in-memory copy of the defaults in local dev
(no DATABASE_URL). Auth stays a shared workspace password, so NO secret is stored
here."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from shiftai_shared.config import SharedSettings

# The five default users. IDs match the studio's task routing; keep them stable.
DEFAULT_USERS: tuple[dict[str, str], ...] = (
    {"id": "aicoe", "name": "AiCoE Admin", "email": "aicoe@levelshift.com",
     "role": "AiCoE Admin"},
    {"id": "marcus", "name": "Ramya Srinivasan", "email": "ramya_s4@levelshift.com",
     "role": "BU Campaign Lead"},
    {"id": "rishi", "name": "Neeraj Vasant Sangani", "email": "neeraj_v@levelshift.com",
     "role": "Marketing Lead"},
    {"id": "jen", "name": "Jen Cook", "email": "jen.cook@levelshift.com",
     "role": "Content Writer"},
    {"id": "tom", "name": "Tom Smith", "email": "tom.smith@levelshift.com",
     "role": "Grammar / Quality Reviewer"},
)


class WorkspaceUser(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    email: str
    role: str
    status: str = "Active"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _new_id() -> str:
    return f"u_{uuid.uuid4().hex[:8]}"


class UserDirectory(Protocol):
    def list_users(self) -> list[WorkspaceUser]: ...
    def create(self, name: str, email: str, role: str) -> WorkspaceUser: ...
    def update(self, user_id: str, patch: dict[str, str]) -> WorkspaceUser | None: ...
    def remove(self, user_id: str) -> bool: ...


class InMemoryUserDirectory:
    """Dev directory (no DATABASE_URL): the defaults plus anything added this
    session. Not persisted, which mirrors the studio's local-only dev behavior."""

    def __init__(self) -> None:
        self._users: dict[str, WorkspaceUser] = {
            u["id"]: WorkspaceUser(**u) for u in DEFAULT_USERS
        }

    def list_users(self) -> list[WorkspaceUser]:
        return sorted(self._users.values(), key=lambda u: u.name)

    def create(self, name: str, email: str, role: str) -> WorkspaceUser:
        user = WorkspaceUser(id=_new_id(), name=name, email=email, role=role, status="Active")
        self._users[user.id] = user
        return user

    def update(self, user_id: str, patch: dict[str, str]) -> WorkspaceUser | None:
        existing = self._users.get(user_id)
        if existing is None:
            return None
        updated = existing.model_copy(update={k: v for k, v in patch.items() if v})
        self._users[user_id] = updated
        return updated

    def remove(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None


class PostgresUserDirectory:
    """Durable directory over the mutable ``users`` table (migration 0005). Ensures
    the table exists and the defaults are seeded, so it is self-sufficient even if
    the migration runner has not been invoked yet."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure()

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(self._dsn, autocommit=True)

    def _ensure(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id text PRIMARY KEY, name text NOT NULL, email text NOT NULL UNIQUE, "
                "role text NOT NULL, status text NOT NULL DEFAULT 'Active', "
                "created_at timestamptz NOT NULL DEFAULT now(), "
                "updated_at timestamptz NOT NULL DEFAULT now())"
            )
            for u in DEFAULT_USERS:
                conn.execute(
                    "INSERT INTO users (id, name, email, role, status) "
                    "VALUES (%s, %s, %s, %s, 'Active') ON CONFLICT (id) DO NOTHING",
                    (u["id"], u["name"], u["email"], u["role"]),
                )

    def list_users(self) -> list[WorkspaceUser]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, email, role, status FROM users ORDER BY name"
            ).fetchall()
        return [
            WorkspaceUser(id=r[0], name=r[1], email=r[2], role=r[3], status=r[4])
            for r in rows
        ]

    def create(self, name: str, email: str, role: str) -> WorkspaceUser:
        user = WorkspaceUser(id=_new_id(), name=name, email=email, role=role, status="Active")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO users (id, name, email, role, status) VALUES (%s,%s,%s,%s,%s)",
                (user.id, user.name, user.email, user.role, user.status),
            )
        return user

    def update(self, user_id: str, patch: dict[str, str]) -> WorkspaceUser | None:
        fields = {k: v for k, v in patch.items() if k in ("name", "email", "role", "status") and v}
        with self._connect() as conn:
            if fields:
                sets = ", ".join(f"{k} = %s" for k in fields)
                conn.execute(
                    f"UPDATE users SET {sets}, updated_at = now() WHERE id = %s",
                    (*fields.values(), user_id),
                )
            row = conn.execute(
                "SELECT id, name, email, role, status FROM users WHERE id = %s", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return WorkspaceUser(id=row[0], name=row[1], email=row[2], role=row[3], status=row[4])

    def remove(self, user_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
            return (cur.rowcount or 0) > 0


def build_user_directory(settings: SharedSettings) -> UserDirectory:
    """Postgres directory when DATABASE_URL is set, else the in-memory defaults."""
    if settings.database_url is not None:
        try:
            return PostgresUserDirectory(settings.database_url.get_secret_value())
        except Exception:
            # A DB hiccup must never take the studio's user list down: fall back to
            # the defaults so the workspace stays usable.
            return InMemoryUserDirectory()
    return InMemoryUserDirectory()
