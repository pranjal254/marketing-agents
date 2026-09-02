"""Task 7 + 8 — deterministic brief assembly with per-field provenance; the
approval gate's structural guarantees."""

from __future__ import annotations

import io
import zipfile

import pytest

from campaign_identification.approval import (
    ApprovalGateError,
    learning_label,
    record,
    scenario_hash,
)
from campaign_identification.brief import assemble_brief, brief_docx, brief_filename
from campaign_identification.intake import normalize_request
from campaign_identification.models import BcFoCheck, Classification, ConflictFlag


def _brief(complete_raw: dict) -> object:
    request = normalize_request(complete_raw, "form", source_ref="form:row-1")
    classification = Classification(
        campaign_type="demand_gen",
        priority="high",
        channel_mix=["events", "email"],
        segment_relevance="type_3",
        field_rationale={"business_unit": "form field business_unit"},
    )
    return assemble_brief(
        case_id="case_x",
        request=request,
        classification=classification,
        conflicts=[
            ConflictFlag(
                kind="duplicate",
                conflicting_campaign_id="cmp_9",
                rationale="same BU/vertical",
                freshness="stale",
            )
        ],
        bc_fo=BcFoCheck(mixed=False),
        normalized_fields={},
        version=1,
    )


def test_every_field_carries_provenance(complete_raw: dict) -> None:
    brief = _brief(complete_raw)
    assert brief.fields  # type: ignore[attr-defined]
    for field in brief.fields:  # type: ignore[attr-defined]
        assert field.provenance.startswith("intake form")
    assert brief.status == "awaiting_approval"  # type: ignore[attr-defined]
    assert brief.template_version == "0.1.0-draft"  # type: ignore[attr-defined]


def test_docx_contains_brief_content(complete_raw: dict) -> None:
    brief = _brief(complete_raw)
    data = brief_docx(brief)  # type: ignore[arg-type]
    document_xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8")
    assert "Campaign Brief" in document_xml
    assert "manufacturing" in document_xml
    assert "cmp_9" in document_xml  # conflict citation present for the approver
    assert "awaiting BU Campaign Lead approval" in document_xml
    assert brief_filename(brief).endswith("-brief-v1.docx")  # type: ignore[arg-type]


def test_approval_requires_identity() -> None:
    with pytest.raises(ApprovalGateError):
        record(decision="approved", actor_role="bu-campaign-lead", actor_id="  ")
    with pytest.raises(ApprovalGateError):
        record(decision="approved", actor_role="", actor_id="x")
    with pytest.raises(ApprovalGateError):
        record(decision="signed_off", actor_role="r", actor_id="x")
    approval = record(decision="approved", actor_role="bu-campaign-lead", actor_id="lead@x.com")
    assert approval.timestamp.endswith("Z")


def test_learning_labels() -> None:
    assert learning_label("route_for_approval", "approved") == "correct"
    assert learning_label("route_for_approval", "rejected") == "false_positive"


def test_scenario_hash_stable_and_scoped(complete_raw: dict) -> None:
    brief = _brief(complete_raw)
    h1 = scenario_hash(brief, "route_for_approval")  # type: ignore[arg-type]
    h2 = scenario_hash(brief, "route_for_approval")  # type: ignore[arg-type]
    assert h1 == h2 and len(h1) == 16
    assert scenario_hash(brief, "request_gaps") != h1  # type: ignore[arg-type]
