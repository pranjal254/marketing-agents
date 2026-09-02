"""Steps 1, 2, 5a: approved-brief gate, intel gathering with fallback, repository
search + deterministic fitness scoring."""

from __future__ import annotations

import pytest
from shiftai_shared.context_store import InMemoryContextStore
from shiftai_shared.semrush import SemrushQuotaError, TopicSignal

from c2c_campaign_box.agent_config import OrchestratorConfig
from c2c_campaign_box.intake import BriefNotApprovedError, load_approved_brief
from c2c_campaign_box.intel import gather_intel
from c2c_campaign_box.repository import LocalRepositoryIndex, RepoQuery, search_all_types
from c2c_campaign_box.workspace import LocalCampaignWorkspace
from tests.conftest import CAMPAIGN_ID, seed_approved_brief

# ------------------------------------------------------------------ step 1


def test_load_approved_brief(store: InMemoryContextStore) -> None:
    brief = load_approved_brief(store, CAMPAIGN_ID)
    assert brief.status == "approved"
    assert brief.topic == "ERP modernization"
    assert brief.window == ("2026-10-15", "2026-12-15")


def test_missing_brief_is_structured_rejection() -> None:
    with pytest.raises(BriefNotApprovedError) as exc:
        load_approved_brief(InMemoryContextStore(), "nope")
    assert exc.value.campaign_id == "nope"


def test_unapproved_brief_rejected() -> None:
    store = InMemoryContextStore()
    seed_approved_brief(store, "cmp_x")
    record = store.get("approved_brief", "cmp_x")
    assert record is not None
    tampered = dict(record.value)
    tampered["brief"] = {**tampered["brief"], "status": "awaiting_approval"}
    store.put("approved_brief", "cmp_x", tampered)
    with pytest.raises(BriefNotApprovedError, match="awaiting_approval"):
        load_approved_brief(store, "cmp_x")


# ------------------------------------------------------------------ step 2


def test_intel_without_semrush_is_flagged_fallback(workspace: LocalCampaignWorkspace) -> None:
    bundle = gather_intel("erp modernization", workspace, None)
    assert bundle.mode == "intel_library_only"
    assert bundle.semrush_failure == "no SemRush API key configured"
    assert bundle.signals, "intel library file must be listed"
    assert all(s.source_uri and s.retrieved_at for s in bundle.signals)


class _QuotaExhaustedSource:
    def topic_signal(self, topic: str) -> TopicSignal:
        raise SemrushQuotaError("ERROR 131 :: LIMIT EXCEEDED")


def test_semrush_failure_falls_back_flagged(workspace: LocalCampaignWorkspace) -> None:
    bundle = gather_intel("erp", workspace, _QuotaExhaustedSource())
    assert bundle.mode == "intel_library_only"
    assert bundle.semrush_failure is not None
    assert "SemrushQuotaError" in bundle.semrush_failure


class _GoodSource:
    def topic_signal(self, topic: str) -> TopicSignal:
        from shiftai_shared.semrush import KeywordStat

        return TopicSignal(
            topic=topic,
            database="us",
            keywords=[
                KeywordStat(
                    phrase=topic, volume=100, cpc=1.0, competition=0.5, results=10,
                    source_uri="semrush://phrase_all?phrase=erp",
                    retrieved_at="2026-09-02T00:00:00Z",
                )
            ],
            related=[],
            organic=[],
        )


def test_semrush_signals_merge_with_library(workspace: LocalCampaignWorkspace) -> None:
    bundle = gather_intel("erp", workspace, _GoodSource())
    assert bundle.mode == "semrush_plus_library"
    origins = {s.origin for s in bundle.signals}
    assert origins == {"semrush", "intel_library"}


# ------------------------------------------------------------------ step 5a


def test_repository_scoring_prefers_matching_candidate(
    repository: LocalRepositoryIndex,
) -> None:
    results = repository.search(
        RepoQuery(
            business_unit="Dynamics",
            vertical="manufacturing",
            topic="ERP modernization",
            asset_type="faq_service_page",
        )
    )
    assert results, "sidecar-matched asset must be found"
    top = results[0]
    assert top.fitness_score > 0.9  # type + vertical + BU + topic all match
    assert set(top.score_breakdown) == {"asset_type", "vertical", "business_unit", "topic"}


def test_unavailable_repository_reports_no_search(
    config: OrchestratorConfig, tmp_path_factory: pytest.TempPathFactory
) -> None:
    missing = LocalRepositoryIndex(
        str(tmp_path_factory.mktemp("empty") / "does-not-exist"), config.fitness_weights
    )
    by_type, performed = search_all_types(
        missing, config, business_unit="Dynamics", vertical="manufacturing", topic="erp"
    )
    assert performed is False
    assert all(v == [] for v in by_type.values())
