"""Context Store interface — versioned, append-only records.

``put`` always inserts a new version; ``get`` returns the latest. There is no update
or delete operation anywhere (append-only audit discipline extends to shared state).
The production binding (Execution Studio's store) implements this same protocol at
onboarding; dev/tests use the local implementations.
"""

from __future__ import annotations

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
