from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store.local_store import InMemoryContextStore, SqliteContextStore
from shiftai_shared.context_store.store import ContextStore, StoredRecord
from shiftai_shared.resilience import IdempotencyStore, SqliteIdempotencyStore

__all__ = [
    "ContextStore",
    "InMemoryContextStore",
    "SqliteContextStore",
    "StoredRecord",
    "build_context_store",
    "build_idempotency_store",
    "store_backend",
]


def store_backend(settings: SharedSettings) -> str:
    """Which backend this process binds: 'postgres' when DATABASE_URL is set."""
    return "postgres" if settings.database_url is not None else "sqlite"


def build_context_store(settings: SharedSettings, sqlite_path: str) -> ContextStore:
    """Backend selection is an environment decision, never a code branch in agents:
    DATABASE_URL set → the tenant-scoped Postgres binding (durable across process
    restarts); unset → the local SQLite binding, exactly as before."""
    if settings.database_url is None:
        return SqliteContextStore(sqlite_path)
    from shiftai_shared.context_store.postgres_store import PostgresContextStore

    return PostgresContextStore(
        settings.database_url.get_secret_value(),
        tenant_id=settings.shiftai_tenant_id,
        client_id=f"c2c-bridge:{settings.shiftai_environment}",
    )


def build_idempotency_store(settings: SharedSettings, sqlite_path: str) -> IdempotencyStore:
    """The side-effect ledger rides the same backend as the Context Store so a
    restart cannot desynchronize state from executed actions."""
    if settings.database_url is None:
        return SqliteIdempotencyStore(sqlite_path)
    from shiftai_shared.context_store.postgres_store import PostgresIdempotencyStore

    return PostgresIdempotencyStore(
        settings.database_url.get_secret_value(),
        tenant_id=settings.shiftai_tenant_id,
        client_id=f"c2c-bridge:{settings.shiftai_environment}",
    )
