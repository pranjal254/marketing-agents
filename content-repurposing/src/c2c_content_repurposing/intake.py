"""Step 1: load the approved outline, audience & offer pack and asset checklist
from the Context Store (written by Agent 2), and validate that every planned
claim in the flagship outline maps to a verified proof point.

Sections whose planned claims cannot be verified are excluded from drafting and
become gap notes — the agent refuses to draft them (spec step 1). Drafting starts
only once the Campaign-in-a-Box plan is confirmed (``in_production``): the spec's
"on outline approval" trigger.
"""

from __future__ import annotations

from dataclasses import dataclass

from c2c_campaign_box import persistence as box_db
from c2c_campaign_box.models import AssetChecklist, AudienceOfferPack, ContentOutline
from shiftai_shared.context_store.store import ContextStore

from c2c_content_repurposing.agent_config import RepurposingConfig
from c2c_content_repurposing.models import GapNote


class PlanNotReadyError(Exception):
    """The Campaign-in-a-Box plan is missing or not yet confirmed — the flagship
    is drafted only from an approved outline (structured rejection, never a guess)."""

    def __init__(self, campaign_id: str, detail: str) -> None:
        super().__init__(f"campaign {campaign_id!r} not ready for drafting: {detail}")
        self.campaign_id = campaign_id
        self.detail = detail


class FlagshipOutlineMissingError(Exception):
    """No approved outline exists for the flagship asset."""

    def __init__(self, campaign_id: str, asset_type: str) -> None:
        super().__init__(
            f"campaign {campaign_id!r} has no approved outline for {asset_type!r}"
        )
        self.campaign_id = campaign_id
        self.asset_type = asset_type


@dataclass(frozen=True)
class DraftingContext:
    """Everything the flagship pass consumes, with claim verification applied."""

    campaign_id: str
    folder: str
    campaign_slug: str
    box_trace_id: str
    pack: AudienceOfferPack
    checklist: AssetChecklist
    flagship_outline: ContentOutline
    outlines: list[ContentOutline]
    verified_refs: set[str]
    draftable_sections: list[dict[str, object]]  # outline sections whose claims verify
    unverified_gap_notes: list[GapNote]


def verified_proof_refs(pack: AudienceOfferPack) -> set[str]:
    """The ONLY refs a flagship claim may cite: verified pack proof points."""
    return {p.source_ref for p in pack.proof_points if p.status == "verified"}


def load_drafting_context(
    store: ContextStore, config: RepurposingConfig, campaign_id: str
) -> DraftingContext:
    case = box_db.load_plan_case(store, campaign_id)
    if case is None:
        raise PlanNotReadyError(campaign_id, "no Campaign-in-a-Box plan exists")
    status = str(case.get("status", ""))
    if status not in {"in_production", "packaging_blocked"}:
        raise PlanNotReadyError(
            campaign_id,
            f"plan status is {status!r}; drafting starts on outline approval "
            "(pack AND plan confirmed)",
        )

    pack_record = store.get(box_db.KIND_PACK, campaign_id)
    checklist_record = store.get(box_db.KIND_CHECKLIST, campaign_id)
    outlines_record = store.get(box_db.KIND_OUTLINES, campaign_id)
    if pack_record is None or checklist_record is None:
        raise PlanNotReadyError(campaign_id, "pack or checklist record missing")
    pack = AudienceOfferPack.model_validate(pack_record.value)
    checklist = AssetChecklist.model_validate(checklist_record.value)
    outlines = [
        ContentOutline.model_validate(o)
        for o in (outlines_record.value.get("outlines", []) if outlines_record else [])
    ]

    flagship_type = config.flagship_asset_type
    flagship_outline = next((o for o in outlines if o.asset_type == flagship_type), None)
    if flagship_outline is None:
        raise FlagshipOutlineMissingError(campaign_id, flagship_type)

    refs = verified_proof_refs(pack)
    draftable: list[dict[str, object]] = []
    gap_notes: list[GapNote] = []
    for index, section in enumerate(flagship_outline.sections):
        unverified = [c for c in section.planned_claims if c not in refs]
        if unverified:
            gap_notes.append(
                GapNote(
                    gap_id=f"gap_{campaign_id}_{index}",
                    asset_id=flagship_type,
                    section=section.heading,
                    needed=(
                        "planned claims without a verified proof point: "
                        + ", ".join(unverified)
                    ),
                )
            )
            continue
        draftable.append(
            {
                "heading": section.heading,
                "notes": section.notes,
                "planned_claims": list(section.planned_claims),
            }
        )

    return DraftingContext(
        campaign_id=campaign_id,
        folder=str(case.get("folder", "")),
        campaign_slug=str(case.get("campaign_slug", "campaign")),
        box_trace_id=str(case.get("trace_id", "")),
        pack=pack,
        checklist=checklist,
        flagship_outline=flagship_outline,
        outlines=outlines,
        verified_refs=refs,
        draftable_sections=draftable,
        unverified_gap_notes=gap_notes,
    )
