"""Dev-session seed data (bridge only, never production): a small sample content
repository and intel library so Agent 2's reuse search and intel gathering have
something real to evaluate in a fresh session. Clearly synthetic content."""

from __future__ import annotations

import json
from pathlib import Path

INTEL_FILES = {
    "manufacturing-erp-modernization-notes.md": (
        "# Curated intel (dev sample)\n\n"
        "Manufacturers in the mid-market report ERP modernization as a top-3 "
        "initiative for 2026; integration debt with legacy systems is the most "
        "cited blocker. (Synthetic dev-session sample file.)"
    ),
    "finserv-ai-adoption-notes.md": (
        "# Curated intel (dev sample)\n\n"
        "Financial services firms prioritize governed AI adoption; compliance "
        "review time is the dominant rollout constraint. (Synthetic dev-session "
        "sample file.)"
    ),
}

REPO_ASSETS = [
    {
        "path": "manufacturing/erp-modernization-faq.docx",
        "content": b"Sample reusable FAQ copy for ERP modernization (dev seed).",
        "meta": {
            "asset_type": "faq_service_page",
            "vertical": "manufacturing",
            "business_unit": "Dynamics",
            "topics": ["erp", "modernization", "dynamics"],
        },
    },
    {
        "path": "manufacturing/erp-one-pager.docx",
        "content": b"Sample external one-pager for ERP campaigns (dev seed).",
        "meta": {
            "asset_type": "external_one_pager",
            "vertical": "manufacturing",
            "business_unit": "Dynamics",
            "topics": ["erp", "modernization"],
        },
    },
    {
        "path": "financial_services/finserv-ai-blog.docx",
        "content": b"Sample flagship blog on governed AI in FinServ (dev seed).",
        "meta": {
            "asset_type": "flagship_blog",
            "vertical": "financial_services",
            "business_unit": "Data360",
            "topics": ["ai", "governance", "finserv"],
        },
    },
]


def seed_dev_workspace(box_workspace: Path, repository: Path) -> None:
    intel_dir = box_workspace / "02-Reference" / "intel-library"
    intel_dir.mkdir(parents=True, exist_ok=True)
    for name, text in INTEL_FILES.items():
        target = intel_dir / name
        if not target.exists():
            target.write_text(text, encoding="utf-8")
    for asset in REPO_ASSETS:
        target = repository / str(asset["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            content = asset["content"]
            assert isinstance(content, bytes)
            target.write_bytes(content)
            sidecar = target.with_name(target.name + ".meta.json")
            sidecar.write_text(json.dumps(asset["meta"], indent=2), encoding="utf-8")
