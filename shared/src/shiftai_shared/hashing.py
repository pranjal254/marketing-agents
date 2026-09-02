"""Content hashing for packaged assets (Agent 2 packaging; Agent 5 verification).

sha256 over the exact bytes that were snapshotted — post-packaging edits are
detectable by re-hash comparison (spec: hash mismatch on a packaged asset halts
the package and escalates to AiCoE).
"""

from __future__ import annotations

import hashlib


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def hashes_match(expected_hex: str, content: bytes) -> bool:
    return sha256_hex(content) == expected_hex
