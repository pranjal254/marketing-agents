"""Kit acceptance criteria row 7, ported: control-plane and engine mechanics must
contain no domain-specific strings and never import agent packages.

Scope note (Agent 2 build): shared/ hosts three kinds of module —
  1. control plane + engine mechanics (kill switch, telemetry, resilience,
     context store, LLM interface, prompting, hashing) — strictly domain-free;
  2. the connector layer (m365/, semrush/) — vendor naming is inherent to a
     connector, but campaign-domain vocabulary stays banned;
  3. versioned Business Capability content (brand/) — domain content BY DESIGN
     (the cross-agent brand rules pack consumed by Agents 2, 3, 5); exempt from
     the vocabulary scan, still barred from importing agent packages.
config.py may name connector env vars (SEMRUSH_*) exactly as it names GRAPH_*.
"""

from __future__ import annotations

import re
from pathlib import Path

SHARED_SRC = Path(__file__).resolve().parents[1] / "src" / "shiftai_shared"

# Domain vocabulary of the Content-to-Campaign business capability. None of it may
# appear in control-plane/engine code (case-insensitive match).
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

# Connector modules: vendor naming allowed, campaign-domain vocabulary still banned.
CONNECTOR_DIRS = {"m365", "semrush"}
CONNECTOR_ALLOWED = {"semrush", "brief"}  # vendor name + generic connector docstrings

# Versioned Business Capability content — domain by design, exempt from the scan.
CAPABILITY_DIRS = {"brand"}

# Settings may declare connector env vars beside GRAPH_*.
CONFIG_ALLOWED = {"semrush"}

AGENT_IMPORT = re.compile(
    r"^\s*(from|import)\s+(campaign_identification|c2c_campaign_box|c2c_bridge)",
    re.MULTILINE,
)


def _python_sources() -> list[Path]:
    return sorted(SHARED_SRC.rglob("*.py"))


def _module_kind(path: Path) -> str:
    parts = set(path.relative_to(SHARED_SRC).parts[:-1])
    if parts & CAPABILITY_DIRS:
        return "capability"
    if parts & CONNECTOR_DIRS:
        return "connector"
    return "core"


def test_no_domain_terms_in_control_plane_and_engine() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        kind = _module_kind(path)
        if kind == "capability":
            continue
        text = path.read_text(encoding="utf-8").lower()
        for term in DOMAIN_TERMS:
            if term not in text:
                continue
            if kind == "connector" and term in CONNECTOR_ALLOWED:
                continue
            if path.name == "config.py" and term in CONFIG_ALLOWED:
                continue
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
