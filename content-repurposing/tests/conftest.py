"""Shared fixtures: hermetic settings (no .env), in-memory stores, a seeded
Campaign-in-a-Box plan (the store contract Agent 2 writes), local workspace and a
scripted mock provider. No live calls anywhere."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import MockLLMProvider
from shiftai_shared.resilience import InMemoryIdempotencyStore
from shiftai_shared.telemetry import InMemorySink

from c2c_content_repurposing.agent_config import RepurposingConfig, load_repurposing_config
from c2c_content_repurposing.orchestration import ContentRepurposingAgent, RepurposingDeps

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "content_repurposing.json"

CAMPAIGN_ID = "cmp_erp_modernization"
FOLDER = "2026-Q4-erp-modernization"
SLUG = "erp-modernization"
TRACE_ID = "trace_agent2_test"

CLAIM_TEXT = "Manufacturers report 42% faster onboarding after ERP modernization"
FLAGSHIP_PARAGRAPH = (
    f"{CLAIM_TEXT} [c-1], and LevelShift pairs that momentum with a single "
    "accountable delivery partner."
)

FLAGSHIP_JSON = json.dumps(
    {
        "title": "ERP modernization without disruption",
        "sections": [
            {"heading": "The problem", "paragraphs": [FLAGSHIP_PARAGRAPH]},
            {
                "heading": "The LevelShift approach",
                "paragraphs": [
                    "ShiftAI keeps every workstream on one accountable plan, "
                    "with human approval at every gate."
                ],
            },
        ],
        "claims_used": [{"marker": "c-1", "claim": CLAIM_TEXT, "source_ref": "sig:1"}],
        "gap_notes": [],
        "confidence": 0.9,
    }
)

INVENTORY_JSON = json.dumps(
    {
        "items": [
            {
                "claim_id": "raw-1",
                "kind": "data_point",
                "text": CLAIM_TEXT,
                "quote": CLAIM_TEXT,
                "source_ref": "sig:1",
            },
            {
                "claim_id": "raw-2",
                "kind": "quote",
                "text": "paraphrased claim that is not verbatim",
                "quote": "this sentence never appears in the flagship",
                "source_ref": "sig:1",
            },
        ],
        "confidence": 0.9,
    }
)

DERIVATIVE_JSON = json.dumps(
    {
        "title": "Channel derivative",
        "variants": [
            {"label": "Variant 1",
             "paragraphs": ["LevelShift customers modernize ERP with confidence.",
                            f"{CLAIM_TEXT}."]},
            {"label": "Variant 2",
             "paragraphs": ["LevelShift keeps momentum without overselling."]},
            {"label": "Variant 3 (over cap)",
             "paragraphs": ["This variant exceeds most volume caps."]},
        ],
        "claims_used": ["cl-1"],
        "gap_notes": [],
        "confidence": 0.85,
    }
)


def seed_box_plan(
    store: InMemoryContextStore,
    campaign_id: str = CAMPAIGN_ID,
    *,
    status: str = "in_production",
    flagship_claims: list[str] | None = None,
) -> None:
    """Seed the records Agent 2 writes once pack AND plan are confirmed."""
    store.put(
        "plan_case",
        campaign_id,
        {
            "status": status,
            "campaign_id": campaign_id,
            "trace_id": TRACE_ID,
            "pack_version": 1,
            "plan_version": 1,
            "checklist_version": 2,
            "folder": FOLDER,
            "campaign_slug": SLUG,
            "confirmations": {"pack": True, "plan": True},
            "updated_at": "2026-09-03T10:00:00Z",
        },
    )
    store.put(
        "audience_offer_pack",
        campaign_id,
        {
            "campaign_id": campaign_id,
            "version": 1,
            "vertical": "manufacturing",
            "personas": [
                {"persona_id": "transformation_mandate_owner",
                 "title": "The Transformation Mandate Owner",
                 "role_pains": "fragmented AI initiatives", "rationale": "brief:objective"}
            ],
            "value_proposition": "ShiftAI-led ERP modernization tied to measurable outcomes",
            "differentiators": ["single accountable partner"],
            "proof_points": [
                {"claim": CLAIM_TEXT, "source_ref": "sig:1", "status": "verified"},
                {"claim": "The brief targets ERP modernization",
                 "source_ref": "brief:offer_topic", "status": "verified"},
                {"claim": "Unverified market figure", "source_ref": "made-up",
                 "status": "unverified"},
            ],
            "ctas": {"awareness": "Read the modernization brief"},
            "messaging_angles": [
                {"persona_id": "transformation_mandate_owner",
                 "angle": "Modernize ERP without disruption", "grounding": "brief:objective"}
            ],
            "channel_emphasis": {"events": "proven channel"},
            "gaps": [],
            "intel_mode": "intel_library_only",
        },
    )
    store.put(
        "asset_checklist",
        campaign_id,
        {
            "campaign_id": campaign_id,
            "version": 2,
            "search_performed": True,
            "items": [
                {"asset_id": "flagship_blog", "asset_type": "flagship_blog",
                 "label": "Flagship blog", "volume": 1, "decision": "create",
                 "decision_rationale": "new angle", "status": "in_production"},
                {"asset_id": "linkedin_posts", "asset_type": "linkedin_posts",
                 "label": "LinkedIn posts", "volume": 2, "decision": "create",
                 "decision_rationale": "net new", "status": "in_production"},
                {"asset_id": "faq_service_page", "asset_type": "faq_service_page",
                 "label": "FAQ / service page", "volume": 1, "decision": "adapt",
                 "decision_rationale": "adapt existing", "status": "in_production"},
                {"asset_id": "battle_card", "asset_type": "battle_card",
                 "label": "Battle card", "volume": 1, "decision": "reuse",
                 "reuse_ref": "repo://battle-card", "decision_rationale": "fits as-is",
                 "status": "in_production"},
            ],
        },
    )
    claims = flagship_claims if flagship_claims is not None else ["sig:1"]
    store.put(
        "content_outlines",
        campaign_id,
        {
            "outlines": [
                {
                    "asset_id": "flagship_blog",
                    "asset_type": "flagship_blog",
                    "title": "ERP modernization without disruption",
                    "sections": [
                        {"heading": "The problem", "notes": "lead with pains",
                         "planned_claims": claims},
                        {"heading": "Unverifiable angle", "notes": "competitor data",
                         "planned_claims": ["made-up-competitor-stat"]},
                    ],
                    "seeded_from_angles": ["transformation_mandate_owner"],
                },
                {
                    "asset_id": "faq_service_page",
                    "asset_type": "faq_service_page",
                    "title": "ERP modernization FAQ",
                    "sections": [
                        {"heading": "What we do", "notes": "Q&A",
                         "planned_claims": ["brief:offer_topic"]}
                    ],
                    "seeded_from_angles": [],
                },
            ]
        },
    )


@pytest.fixture()
def config() -> RepurposingConfig:
    return load_repurposing_config(CONFIG_PATH)


@pytest.fixture()
def settings() -> SharedSettings:
    return SharedSettings(_env_file=None)


@pytest.fixture()
def store() -> InMemoryContextStore:
    s = InMemoryContextStore()
    seed_box_plan(s)
    return s


@pytest.fixture()
def workspace(tmp_path: Path) -> LocalCampaignWorkspace:
    return LocalCampaignWorkspace(str(tmp_path / "box-workspace"))


@pytest.fixture()
def provider() -> MockLLMProvider:
    return MockLLMProvider(
        script=[
            (lambda u: "Draft the flagship asset" in u, FLAGSHIP_JSON),
            (lambda u: u.startswith("Extract the confirmed flagship's claim inventory"),
             INVENTORY_JSON),
            (lambda u: "derivative from the claim inventory" in u, DERIVATIVE_JSON),
        ],
        default="{}",
        model_name="mock-model",
    )


@pytest.fixture()
def sink() -> InMemorySink:
    return InMemorySink()


def build_agent(
    provider: MockLLMProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
    *,
    kill_switch: KillSwitch | None = None,
) -> ContentRepurposingAgent:
    return ContentRepurposingAgent(
        RepurposingDeps(
            provider=provider,
            store=store,
            workspace=workspace,
            sink=sink,
            kill_switch=kill_switch or KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=50),
            idempotency=InMemoryIdempotencyStore(),
            config=config,
            settings=settings,
            brand_rules=load_brand_rules(),
        )
    )


@pytest.fixture()
def agent(
    provider: MockLLMProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: RepurposingConfig,
    settings: SharedSettings,
) -> ContentRepurposingAgent:
    return build_agent(provider, store, workspace, sink, config, settings)


def events_of(sink: InMemorySink, event_type: str) -> list[dict[str, Any]]:
    return [r for r in sink.records if r.get("shiftai.event.type") == event_type]
