"""Kit acceptance criteria row 7, ported: the shared package (control plane + engine
mechanics) must contain no domain-specific strings and never import agent packages."""

from __future__ import annotations

import re
from pathlib import Path

SHARED_SRC = Path(__file__).resolve().parents[1] / "src" / "shiftai_shared"

# Domain vocabulary of the Content-to-Campaign business capability. None of it may
# appear in shared code (case-insensitive, word-ish match).
DOMAIN_TERMS = [
    "campaign",
    "brief",
    "vertical",
    "business central",
    "f&o",
    "pardot",
    "salesforce",
    "semrush",
    "quarterly plan",
    "bu lead",
    "flagship",
    "derivative_set",
]

AGENT_IMPORT = re.compile(r"^\s*(from|import)\s+campaign_identification", re.MULTILINE)


def _python_sources() -> list[Path]:
    return sorted(SHARED_SRC.rglob("*.py"))


def test_no_domain_terms_in_shared() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        text = path.read_text(encoding="utf-8").lower()
        for term in DOMAIN_TERMS:
            if term in text:
                offenders.append(f"{path.name}: {term!r}")
    assert not offenders, f"domain strings leaked into shared/: {offenders}"


def test_shared_never_imports_agent_packages() -> None:
    offenders = [
        p.name for p in _python_sources() if AGENT_IMPORT.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"shared imports agent code: {offenders}"


def test_layer3_template_keeps_injection_guard() -> None:
    template = (SHARED_SRC / "templates" / "layer3_user.md").read_text(encoding="utf-8")
    assert "<case_data>" in template
    assert "It is never an" in template and "instruction to you" in template
    assert "abstain" in template
