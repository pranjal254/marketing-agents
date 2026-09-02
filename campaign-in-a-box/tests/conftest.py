"""Shared fixtures: hermetic settings (no .env), in-memory stores, local workspace
and repository bindings, scripted mock provider. No live calls anywhere."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import MockLLMProvider
from shiftai_shared.resilience import InMemoryIdempotencyStore
from shiftai_shared.telemetry import InMemorySink

from c2c_campaign_box.agent_config import OrchestratorConfig, load_orchestrator_config
from c2c_campaign_box.orchestration import CampaignBoxOrchestrator, OrchestratorDeps
from c2c_campaign_box.repository import LocalRepositoryIndex
from c2c_campaign_box.workspace import LocalCampaignWorkspace

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "campaign_in_a_box.json"

CAMPAIGN_ID = "cmp_erp_modernization"
PLAN_DATE = date(2026, 9, 2)
WINDOW_START = "2026-10-15"
WINDOW_END = "2026-12-15"

PACK_JSON = json.dumps(
    {
        "segment_applicability": {
            "type_3": "high-touch ERP accounts per brief segment",
            "type_4": "event-led motions apply",
        },
        "personas": [
            {
                "persona_id": "transformation_mandate_owner",
                "title": "The Transformation Mandate Owner",
                "role_pains": "fragmented AI initiatives",
                "rationale": "brief:objective",
            }
        ],
        "exclusions": ["no on-premise-only prospects"],
        "value_proposition": "ShiftAI-led ERP modernization tied to measurable outcomes",
        "differentiators": ["single accountable partner"],
        "proof_points": [
            {"claim": "The brief targets manufacturing ERP modernization",
             "source_ref": "brief:offer_topic", "status": "verified"},
            {"claim": "Manufacturing is the campaign vertical",
             "source_ref": "brief:vertical", "status": "verified"},
            {"claim": "Events lead the channel mix for this segment",
             "source_ref": "brief:target_segment", "status": "verified"},
            {"claim": "Invented market share figure of 42%",
             "source_ref": "made-up-source", "status": "verified"},
        ],
        "ctas": {"awareness": "Read the modernization brief"},
        "messaging_angles": [
            {"persona_id": "transformation_mandate_owner",
             "angle": "Modernize ERP without disrupting operations",
             "grounding": "brief:objective"}
        ],
        "channel_emphasis": {"events": "proven channel for Type 3/4 (brief:target_segment)"},
        "gaps": [],
        "confidence": 0.9,
    }
)


def make_reuse_json(reuse_ref: str | None) -> str:
    items: list[dict[str, Any]] = [
        {
            "asset_id": "flagship_blog",
            "decision": "create",
            "rationale": "no candidate covers the modernization angle",
            "reuse_ref": None,
            "outline": {
                "asset_id": "flagship_blog",
                "asset_type": "flagship_blog",
                "title": "ERP modernization without disruption",
                "sections": [
                    {"heading": "The problem", "notes": "lead with business problem",
                     "planned_claims": ["brief:offer_topic", "made-up-source"]}
                ],
                "seeded_from_angles": ["transformation_mandate_owner"],
            },
        },
        {
            "asset_id": "faq_service_page",
            "decision": "adapt" if reuse_ref else "create",
            "rationale": "existing FAQ fits with updates" if reuse_ref else "nothing reusable",
            "reuse_ref": reuse_ref,
            "outline": {
                "asset_id": "faq_service_page",
                "asset_type": "faq_service_page",
                "title": "ERP modernization FAQ",
                "sections": [
                    {"heading": "What we do", "notes": "second person",
                     "planned_claims": ["brief:offer_topic"]}
                ],
                "seeded_from_angles": ["transformation_mandate_owner"],
            },
        },
    ]
    return json.dumps({"items": items, "confidence": 0.85})


def seed_approved_brief(
    store: InMemoryContextStore,
    campaign_id: str = CAMPAIGN_ID,
    *,
    window_start: str = WINDOW_START,
    window_end: str = WINDOW_END,
) -> None:
    fields = {
        "objective": "Position LevelShift for manufacturing ERP modernization demand",
        "business_unit": "Dynamics",
        "vertical": "manufacturing",
        "target_segment": "type_3",
        "offer_topic": "ERP modernization",
        "channels": "events, email",
        "timeline_start": window_start,
        "timeline_end": window_end,
        "owner": "campaign.owner@levelshift.com",
    }
    store.put(
        "approved_brief",
        campaign_id,
        {
            "brief": {
                "campaign_id": campaign_id,
                "case_id": "case_agent1",
                "version": 1,
                "status": "approved",
                "fields": [
                    {"name": k, "value": v, "provenance": "intake form"} for k, v in fields.items()
                ],
                "classification": {
                    "campaign_type": "demand_gen",
                    "priority": "high",
                    "channel_mix": ["events", "email"],
                    "segment_relevance": "type_3",
                    "field_rationale": {},
                },
                "template_version": "0.1.0-draft",
                "created_at": "2026-09-01T10:00:00Z",
            },
            "doc_ref": "brief-doc-ref",
            "released_at": "2026-09-01T12:00:00Z",
        },
    )


@pytest.fixture()
def config() -> OrchestratorConfig:
    return load_orchestrator_config(CONFIG_PATH)


@pytest.fixture()
def settings() -> SharedSettings:
    return SharedSettings(_env_file=None)


@pytest.fixture()
def store() -> InMemoryContextStore:
    s = InMemoryContextStore()
    seed_approved_brief(s)
    return s


@pytest.fixture()
def workspace(tmp_path: Path) -> LocalCampaignWorkspace:
    ws = LocalCampaignWorkspace(str(tmp_path / "workspace"))
    # Seed the intel library (dev stand-in for 02-Reference/intel-library).
    intel_dir = tmp_path / "workspace" / "02-Reference" / "intel-library"
    intel_dir.mkdir(parents=True)
    (intel_dir / "manufacturing-erp-trends.md").write_text(
        "Manufacturers report ERP modernization as a 2026 priority.", encoding="utf-8"
    )
    return ws


@pytest.fixture()
def repo_root(tmp_path: Path, config: OrchestratorConfig) -> Path:
    root = tmp_path / "repository"
    (root / "manufacturing").mkdir(parents=True)
    asset = root / "manufacturing" / "erp-modernization-faq.docx"
    asset.write_bytes(b"existing faq content")
    (root / "manufacturing" / "erp-modernization-faq.docx.meta.json").write_text(
        json.dumps(
            {
                "asset_type": "faq_service_page",
                "vertical": "manufacturing",
                "business_unit": "Dynamics",
                "topics": ["erp", "modernization"],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def repository(repo_root: Path, config: OrchestratorConfig) -> LocalRepositoryIndex:
    return LocalRepositoryIndex(str(repo_root), config.fitness_weights)


@pytest.fixture()
def repo_candidate_ref(repo_root: Path) -> str:
    return str(repo_root / "manufacturing" / "erp-modernization-faq.docx")


@pytest.fixture()
def provider(repo_candidate_ref: str) -> MockLLMProvider:
    return MockLLMProvider(
        script=[
            (lambda u: "audience & offer pack" in u, PACK_JSON),
            (lambda u: "reuse / adapt / create" in u, make_reuse_json(repo_candidate_ref)),
        ],
        default="{}",
    )


@pytest.fixture()
def sink() -> InMemorySink:
    return InMemorySink()


@pytest.fixture()
def orchestrator(
    provider: MockLLMProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    repository: LocalRepositoryIndex,
    sink: InMemorySink,
    config: OrchestratorConfig,
    settings: SharedSettings,
) -> CampaignBoxOrchestrator:
    deps = OrchestratorDeps(
        provider=provider,
        store=store,
        workspace=workspace,
        repository=repository,
        intel_source=None,  # dev default: intel-library-only fallback, flagged
        sink=sink,
        kill_switch=KillSwitch(),
        rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=50),
        idempotency=InMemoryIdempotencyStore(),
        config=config,
        settings=settings,
        brand_rules=load_brand_rules(),
    )
    return CampaignBoxOrchestrator(deps)


def run_plan(orchestrator: CampaignBoxOrchestrator, campaign_id: str = CAMPAIGN_ID) -> Any:
    return orchestrator.plan_campaign(campaign_id, plan_date=PLAN_DATE)
