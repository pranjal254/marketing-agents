"""Context Store interface — versioned, append-only records.

``put`` always inserts a new version; ``get`` returns the latest. There is no update
or delete operation anywhere (append-only audit discipline extends to shared state).
The production binding (Execution Studio's store) implements this same protocol at
onboarding; dev/tests use the local implementations.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class StoredRecord:
    kind: str
    key: str
    version: int
    value: dict[str, Any]
    created_at: str


class ContextStore(Protocol):
    def put(self, kind: str, key: str, value: dict[str, Any]) -> StoredRecord: ...

    def get(self, kind: str, key: str) -> StoredRecord | None: ...

    def get_all_versions(self, kind: str, key: str) -> list[StoredRecord]: ...

    def query(self, kind: str) -> list[StoredRecord]:
        """Latest version of every key of ``kind``."""
        ...


def latest_many(
    store: ContextStore, kind_keys: Sequence[tuple[str, str]]
) -> dict[tuple[str, str], StoredRecord]:
    """The latest version of several records in as few round trips as possible.

    Reading one screen often means reading six or seven records that share a
    key. Done one ``get`` at a time against a hosted database in another
    region, the latency adds up into seconds of blank screen, and every one of
    those calls also serialises behind the binding's connection lock.

    A backend may expose ``get_many`` to answer the whole set in one statement;
    one that does not simply gets the obvious loop, so local SQLite and the
    in-memory test store need no change and behave identically.

    Missing records are absent from the result rather than present as None, so
    a caller distinguishes "not written yet" without a second check.
    """
    batch = getattr(store, "get_many", None)
    if callable(batch):
        return dict(batch(list(kind_keys)))
    found: dict[tuple[str, str], StoredRecord] = {}
    for kind, key in kind_keys:
        record = store.get(kind, key)
        if record is not None:
            found[(kind, key)] = record
    return found
