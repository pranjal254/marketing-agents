"""Shared fixtures: hermetic settings, in-memory store seeded with the records
agents 2-3 write, local workspace, scripted mock provider, recording signals.
No live calls anywhere."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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

from c2c_collaboration.agent_config import CollaborationConfig, load_collaboration_config
from c2c_collaboration.orchestration import CollaborationAgent, CollaborationDeps

AGENT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = AGENT_ROOT / "config" / "collaboration_iteration.json"

CAMPAIGN_ID = "cmp_review_test"
FOLDER = "2026-Q4-review-test"
SLUG = "review-test"
TRACE_ID = "trace_box_test"

MARKER_SENTENCE = "Retailers report 42% faster planning cycles [c-1]."

def _case_data(user: str) -> dict[str, object]:
    # The injection-guard sentence mentions the tag too — take the LAST opening tag.
    payload = user.rsplit("<case_data>", 1)[1].split("</case_data>", 1)[0]
    return dict(json.loads(payload))


class ScriptedReviewProvider:
    """Deterministic stand-in that behaves like a competent reviewer-model: it
    classifies by the comment's text and echoes the REAL generated feedback ids
    (they are random uuids, so canned JSON cannot be used)."""

    model_name = "mock-model"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.consolidation_reply: str | None = None  # override to force degrade
        self.drop_feedback_containing: str | None = None  # simulate a lost item

    def complete(self, *, system: object, user: str, model: str, max_tokens: int,
                 temperature: float = 0.0, timeout_s: float = 60.0) -> object:
        from shiftai_shared.llm import LLMResponse

        self.calls.append(user)
        text = self._reply(user)
        return LLMResponse(text=text, model=self.model_name,
                           input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                           cache_read_input_tokens=0, finish_reason="end_turn")

    def _reply(self, user: str) -> str:
        if user.startswith("Consolidate this round's reviewer feedback"):
            if self.consolidation_reply is not None:
                return self.consolidation_reply
            data = _case_data(user)
            raw_items = data.get("feedback_items", [])
            assert isinstance(raw_items, list)
            by_text = {str(i["text"]): i for i in raw_items}
            casual = by_text.get("Make the hook casual")
            formal = by_text.get("Keep the hook formal")
            out = []
            for item in raw_items:
                text = str(item["text"])
                if self.drop_feedback_containing and self.drop_feedback_containing in text:
                    continue
                row: dict[str, object] = {
                    "feedback_id": item["feedback_id"],
                    "location": item.get("section", ""),
                    "instruction": text,
                    "reviewer": item.get("reviewer", ""),
                    "type": "textual",
                    "rationale": "copy edit",
                }
                if "Rebuild" in text:
                    row["type"] = "structural"
                    row["rationale"] = "needs regeneration"
                elif "webinar" in text:
                    row["type"] = "out_of_scope"
                    row["rationale"] = "beyond this asset"
                elif "casual" in text and formal is not None:
                    row["conflicts_with"] = formal["feedback_id"]
                elif "formal" in text and casual is not None:
                    row["conflicts_with"] = casual["feedback_id"]
                out.append(row)
            return json.dumps({"items": out, "confidence": 0.9})
        if user.startswith("Apply ONLY the textual edits"):
            data = _case_data(user)
            edits = data.get("edits_to_apply", [])
            assert isinstance(edits, list)
            applied = [str(e["feedback_id"]) for e in edits]
            return json.dumps({
                "sections": [
                    {"heading": "Hook",
                     "paragraphs": ["Retailers report 50% faster planning cycles [c-1].",
                                    "A tighter second line."]},
                    {"heading": "CTA", "paragraphs": ["Talk to LevelShift today."]},
                ],
                "applied": applied,
                "deferred": [],
                "edit_summary": "Tightened the CTA; attempted the statistic change.",
                "confidence": 0.9,
            })
        return "{}"


class RecordingSignals:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.raise_on: set[str] = set()

    def flagship_confirmed(self, campaign_id: str, actor_id: str, actor_role: str) -> None:
        if "flagship_confirmed" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("flagship_confirmed", (campaign_id, actor_id, actor_role)))

    def register_confirmed(
        self, campaign_id: str, asset_id: str, actor_id: str, actor_role: str
    ) -> None:
        if "register_confirmed" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("register_confirmed", (campaign_id, asset_id, actor_id, actor_role)))

    def route_rework(
        self, campaign_id: str, asset_id: str, instruction: str, actor_id: str
    ) -> None:
        if "route_rework" in self.raise_on:
            raise RuntimeError("signal down")
        self.calls.append(("route_rework", (campaign_id, asset_id, instruction, actor_id)))


def seed_campaign(store: InMemoryContextStore, campaign_id: str = CAMPAIGN_ID) -> None:
    store.put("plan_case", campaign_id, {
        "status": "in_production", "campaign_id": campaign_id, "trace_id": TRACE_ID,
        "folder": FOLDER, "campaign_slug": SLUG,
        "confirmations": {"pack": True, "plan": True},
    })
    store.put("asset_checklist", campaign_id, {
        "campaign_id": campaign_id, "version": 2, "search_performed": True,
        "items": [
            {"asset_id": "flagship_blog", "asset_type": "flagship_blog",
             "label": "Flagship blog", "volume": 1, "decision": "create",
             "decision_rationale": "new", "status": "in_production"},
            {"asset_id": "linkedin_posts", "asset_type": "linkedin_posts",
             "label": "LinkedIn posts", "volume": 2, "decision": "create",
             "decision_rationale": "new", "status": "in_production"},
            {"asset_id": "battle_card", "asset_type": "battle_card",
             "label": "Battle card", "volume": 1, "decision": "reuse",
             "reuse_ref": "repo://bc", "decision_rationale": "fits", "status": "in_production"},
        ],
    })
    store.put("workflow_plan", campaign_id, {
        "campaign_id": campaign_id, "version": 1,
        "window_start": "2026-10-01", "window_end": "2026-12-01",
        "feasible": True, "capacity_note": "", "infeasibility": None,
        "entries": [
            {"asset_id": "flagship_blog", "asset_type": "flagship_blog",
             "draft_due": "2026-10-05", "review_due": "2026-10-08",
             "confirm_due": "2026-10-09", "review_gate": "flagship",
             "constraint_chain": "test"},
            {"asset_id": "linkedin_posts", "asset_type": "linkedin_posts",
             "draft_due": "2026-10-10", "review_due": "2026-10-13",
             "confirm_due": "2026-10-14", "review_gate": "derivative",
             "constraint_chain": "test"},
        ],
    })


def make_draft(asset_id: str, asset_type: str, *, version: int = 1,
               kind: str = "derivative") -> StagedDraft:
    return StagedDraft(
        campaign_id=CAMPAIGN_ID, asset_id=asset_id, asset_type=asset_type,
        kind="flagship" if kind == "flagship" else "derivative",
        title=f"Test {asset_type}", version=version,
        filename=f"{SLUG}-{asset_type.replace('_', '-')}-v{version}.docx",
        file_ref=f"/tmp/{asset_id}-v{version}.docx", claim_map_ref="",
        sections=[
            DraftSection(heading="Hook",
                         paragraphs=[MARKER_SENTENCE, "A second line to edit."]),
            DraftSection(heading="CTA", paragraphs=["Please talk to LevelShift now."]),
        ],
        claim_markers=[ClaimMarker(marker="c-1",
                                   claim="Retailers report 42% faster planning cycles",
                                   source_ref="sig:1")],
        claim_lineage=["cl-1"],
        self_check=SelfCheckReport(passed=True, attempts=1),
        status="staged", created_at="2026-09-03T10:00:00Z",
    )


@pytest.fixture()
def config() -> CollaborationConfig:
    return load_collaboration_config(CONFIG_PATH)


@pytest.fixture()
def settings() -> SharedSettings:
    return SharedSettings(_env_file=None)


@pytest.fixture()
def store() -> InMemoryContextStore:
    s = InMemoryContextStore()
    seed_campaign(s)
    save_draft(s, make_draft("linkedin_posts", "linkedin_posts"))
    save_draft(s, make_draft("flagship_blog", "flagship_blog", kind="flagship"))
    return s


@pytest.fixture()
def workspace(tmp_path: Path) -> LocalCampaignWorkspace:
    return LocalCampaignWorkspace(str(tmp_path / "box-workspace"))


@pytest.fixture()
def provider() -> ScriptedReviewProvider:
    return ScriptedReviewProvider()


@pytest.fixture()
def signals() -> RecordingSignals:
    return RecordingSignals()


def build_agent(
    provider: ScriptedReviewProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: CollaborationConfig,
    settings: SharedSettings,
    signals: RecordingSignals,
) -> CollaborationAgent:
    return CollaborationAgent(
        CollaborationDeps(
            provider=provider, store=store, workspace=workspace, sink=sink,
            kill_switch=KillSwitch(),
            rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=50),
            idempotency=InMemoryIdempotencyStore(),
            config=config, settings=settings, brand_rules=load_brand_rules(),
            signals=signals,
        )
    )


@pytest.fixture()
def sink() -> InMemorySink:
    return InMemorySink()


@pytest.fixture()
def agent(
    provider: ScriptedReviewProvider,
    store: InMemoryContextStore,
    workspace: LocalCampaignWorkspace,
    sink: InMemorySink,
    config: CollaborationConfig,
    settings: SharedSettings,
    signals: RecordingSignals,
) -> CollaborationAgent:
    return build_agent(provider, store, workspace, sink, config, settings, signals)


def add_default_feedback(agent: CollaborationAgent) -> dict[str, str]:
    """Six comments matching DEFAULT_CONSOLIDATION's feedback ids."""
    mapping = {
        "fb-cta": ("jen", "content-writer", "CTA", "Make the CTA shorter"),
        "fb-marker": ("jen", "content-writer", "Hook", "Change 42% to 50%"),
        "fb-structural": ("rishi", "marketing-lead", "",
                          "Rebuild the piece around governance-first framing"),
        "fb-oos": ("jen", "content-writer", "", "Add a webinar series"),
        "fb-casual": ("jen", "content-writer", "Hook", "Make the hook casual"),
        "fb-formal": ("rishi", "marketing-lead", "Hook", "Keep the hook formal"),
    }
    ids: dict[str, str] = {}
    for wanted, (rid, role, section, text) in mapping.items():
        item = agent.add_feedback(CAMPAIGN_ID, "linkedin_posts", reviewer_id=rid,
                                  reviewer_role=role, section=section, text=text)
        ids[wanted] = item.feedback_id
    return ids


def events_of(sink: InMemorySink, event_type: str) -> list[dict[str, object]]:
    return [r for r in sink.records if r.get("shiftai.event.type") == event_type]
