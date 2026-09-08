"""Shared fixtures: hermetic settings, in-memory store seeded with the records
agents 2-4 write (plan case, checklist, staged drafts, package manifest), local
workspace holding real snapshot bytes, scripted mock provider, recording
signals. No live calls anywhere."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from c2c_campaign_box.models import PackagedAsset, PackageManifest
from c2c_campaign_box.persistence import KIND_MANIFEST
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from c2c_content_repurposing.models import (
    ClaimMarker,
    DraftSection,
    SelfCheckReport,
    StagedDraft,
)
from c2c_content_repurposing.persistence import save_draft
from shiftai_shared.brand import load_brand_rules
from shiftai_shared.config import SharedSettings
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.resilience import InMemoryIdempotencyStore
from shiftai_shared.telemetry import InMemorySink

from c2c_quality_gate.agent_config import QualityGateConfig, load_quality_gate_config
from c2c_quality_gate.models import CONTEXTUAL_RULES
from c2c_quality_gate.orchestration import QualityGateAgent, QualityGateDeps

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "quality_gate.json"

CAMPAIGN_ID = "cmp_gate_test"
FOLDER = "2026-Q4-gate-test"
SLUG = "gate-test"
TRACE_ID = "trace_gate_test"

CLEAN_TEXT = (
    "Retailers report 42% faster planning cycles [c-1].\n"
    "LevelShift helps manufacturers modernize ERP with measurable outcomes.\n"
    "Talk to LevelShift about a scoped assessment."
)
INTERNAL_TEXT = (
    "Objection: too costly. Response: retailers report 42% faster planning "
    "cycles [c-1]. Next step: offer the assessment."
)

CLEAN_CONTEXTUAL = json.dumps(
    {"findings": [], "checks_completed": sorted(CONTEXTUAL_RULES), "confidence": 0.9}
)


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ScriptedGateProvider:
    """Deterministic contextual-pass stand-in. Default: a clean report naming
    every rule as completed. Override ``reply`` (or ``reply_for`` by asset_id)
    to force findings, omissions, or garbage."""

    model_name = "mock-model"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.reply: str | None = None
        self.reply_for: dict[str, str] = {}

    def complete(self, *, system: object, user: str, model: str, max_tokens: int,
                 temperature: float = 0.0, timeout_s: float = 60.0) -> object:
        from shiftai_shared.llm import LLMResponse

        self.calls.append(user)
        text = self.reply if self.reply is not None else CLEAN_CONTEXTUAL
        for asset_id, reply in self.reply_for.items():
            if f'"asset_id": "{asset_id}"' in user:
                text = reply
        return LLMResponse(text=text, model=self.model_name,
                           input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                           cache_read_input_tokens=0, finish_reason="end_turn")


class RecordingSignals:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.raise_on: set[str] = set()

    def assets_failed(self, campaign_id: str, findings_by_asset: dict[str, list[str]]) -> None:
        if "assets_failed" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("assets_failed", (campaign_id, findings_by_asset)))

    def package_returned(
        self, campaign_id: str, asset_ids: list[str], notes: str, actor_id: str
    ) -> None:
        if "package_returned" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("package_returned", (campaign_id, asset_ids, notes, actor_id)))

    def package_approved(self, campaign_id: str, manifest_version: int) -> None:
        if "package_approved" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("package_approved", (campaign_id, manifest_version)))


def seed_campaign(store: InMemoryContextStore) -> None:
    store.put("plan_case", CAMPAIGN_ID, {
        "status": "packaged_pending_compliance", "campaign_id": CAMPAIGN_ID,
        "trace_id": TRACE_ID, "folder": FOLDER, "campaign_slug": SLUG,
        "confirmations": {"pack": True, "plan": True},
    })
    for asset_id, asset_type in (
        ("flagship_blog", "flagship_blog"),
        ("linkedin_posts", "linkedin_posts"),
        ("battle_card", "battle_card"),
    ):
        save_draft(store, StagedDraft(
            campaign_id=CAMPAIGN_ID, asset_id=asset_id, asset_type=asset_type,
            kind="flagship" if asset_type == "flagship_blog" else "derivative",
            title=f"Test {asset_type}", version=1,
            filename=f"{SLUG}-{asset_type.replace('_', '-')}-v1.docx",
            file_ref=f"/tmp/{asset_id}-v1.docx", claim_map_ref="",
            sections=[DraftSection(heading="Body", paragraphs=[CLEAN_TEXT])],
            claim_markers=[ClaimMarker(marker="c-1",
                                       claim="Retailers report 42% faster planning cycles",
                                       source_ref="sig:1")],
            claim_lineage=["cl-1"],
            self_check=SelfCheckReport(passed=True, attempts=1),
            status="staged", created_at="2026-09-03T10:00:00Z",
        ))


def seed_manifest(
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    contents: dict[str, bytes] | None = None,
    *,
    tamper_hash_for: str | None = None,
) -> PackageManifest:
    """Upload snapshot bytes and register the packaged_pending_compliance manifest
    exactly as Agent 2's packaging module does."""
    contents = contents or {
        "flagship_blog": CLEAN_TEXT.encode(),
        "linkedin_posts": CLEAN_TEXT.encode(),
        "battle_card": INTERNAL_TEXT.encode(),
    }
    types = {"flagship_blog": "flagship_blog", "linkedin_posts": "linkedin_posts",
             "battle_card": "battle_card", "faq_service_page": "faq_service_page"}
    assets: list[PackagedAsset] = []
    for asset_id, content in contents.items():
        name = f"{SLUG}-{asset_id.replace('_', '-')}-v1"
        ref = workspace.upload(f"{FOLDER}/confirmed", f"{name}.docx", content)
        digest = sha(content)
        if tamper_hash_for == asset_id:
            digest = "0" * 64
        assets.append(PackagedAsset(
            asset_id=asset_id, asset_type=types[asset_id], canonical_name=f"{name}.docx",
            source_ref=ref, snapshot_ref=ref, version=1, sha256=digest,
        ))
    manifest = PackageManifest(
        manifest_id="man-1", campaign_id=CAMPAIGN_ID, version=1, assets=assets,
        checklist_version=2, created_at="2026-09-04T10:00:00Z",
    )
    store.put(KIND_MANIFEST, CAMPAIGN_ID, manifest.model_dump())
    return manifest


@pytest.fixture()
def config() -> QualityGateConfig:
    return load_quality_gate_config(CONFIG_PATH)


@pytest.fixture()
def settings() -> SharedSettings:
    return SharedSettings(_env_file=None)


@pytest.fixture()
def store() -> InMemoryContextStore:
    s = InMemoryContextStore()
    seed_campaign(s)
    return s


@pytest.fixture()
def workspace(tmp_path: Path) -> LocalCampaignWorkspace:
    return LocalCampaignWorkspace(str(tmp_path / "box-workspace"))


@pytest.fixture()
def provider() -> ScriptedGateProvider:
    return ScriptedGateProvider()


@pytest.fixture()
def signals() -> RecordingSignals:
    return RecordingSignals()


@pytest.fixture()
def sink() -> InMemorySink:
    return InMemorySink()


def build_agent(
    provider: ScriptedGateProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: QualityGateConfig,
    settings: SharedSettings,
    signals: RecordingSignals,
) -> QualityGateAgent:
    return QualityGateAgent(
        QualityGateDeps(
            provider=provider, store=store, workspace=workspace, sink=sink,
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
            idempotency=InMemoryIdempotencyStore(),
            config=config, settings=settings, brand_rules=load_brand_rules(),
            signals=signals,
        )
    )


@pytest.fixture()
def agent(
    provider: ScriptedGateProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: QualityGateConfig,
    settings: SharedSettings,
    signals: RecordingSignals,
) -> QualityGateAgent:
    return build_agent(provider, store, workspace, sink, config, settings, signals)


def approve_all_asset_tasks(agent: QualityGateAgent) -> None:
    from c2c_quality_gate import persistence as db

    for task in db.load_tasks(agent.deps.store, CAMPAIGN_ID):
        if task.scope == "asset" and task.status == "open":
            agent.record_review_outcome(
                CAMPAIGN_ID, task.task_id, decision="approved",
                actor_id="qa@x", actor_role=task.role,
            )


def events_of(sink: InMemorySink, event_type: str) -> list[dict[str, object]]:
    return [r for r in sink.records if r.get("shiftai.event.type") == event_type]
