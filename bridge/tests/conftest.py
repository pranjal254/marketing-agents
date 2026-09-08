"""Bridge test hermeticity: the module-level app entry loads Agents/.env into the
process environment for dev runs, so deployment-binding vars set on the
developer's machine (C2C_*) must never leak into tests — each test opts in
explicitly via monkeypatch.setenv."""

from __future__ import annotations

import pytest

_BINDING_VARS = (
    "C2C_MARKETING_ROOT",
    "C2C_BRAND_PACK",
    "C2C_BOX_CONFIG",
    "C2C_REPURPOSE_CONFIG",
    "C2C_COLLAB_CONFIG",
)


@pytest.fixture(autouse=True)
def _hermetic_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _BINDING_VARS:
        monkeypatch.delenv(name, raising=False)
