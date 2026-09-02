"""Loads the STS v2.0.0 JSON Schema from the starter kit — the single source of truth.

Resolution order: explicit path argument → STS_SCHEMA_PATH env var → upward search
from this file for ``levelshift-agent-starter-kit/schemas/sts-core.schema.v2.0.0.json``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA_RELPATH = Path("levelshift-agent-starter-kit") / "schemas" / "sts-core.schema.v2.0.0.json"


def _search_upward(start: Path) -> Path | None:
    for parent in [start, *start.parents]:
        candidate = parent / SCHEMA_RELPATH
        if candidate.is_file():
            return candidate
    return None


def find_schema_path(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"STS schema not found at explicit path: {explicit}")
    env = os.environ.get("STS_SCHEMA_PATH")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise FileNotFoundError(f"STS schema not found at STS_SCHEMA_PATH: {env}")
    found = _search_upward(Path(__file__).resolve()) or _search_upward(Path.cwd().resolve())
    if found is None:
        raise FileNotFoundError(
            "sts-core.schema.v2.0.0.json not found; set STS_SCHEMA_PATH or keep the "
            "starter kit alongside the agent packages"
        )
    return found


@lru_cache(maxsize=4)
def _load_cached(path_str: str) -> dict[str, Any]:
    with open(path_str, encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def load_sts_schema(explicit_path: str | None = None) -> dict[str, Any]:
    return _load_cached(str(find_schema_path(explicit_path)))
