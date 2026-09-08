"""Task 7 - assemble the structured campaign brief (deterministic packaging of the
validated request + classification; the Word document is built with no LLM).

The document follows the marketing team's standard Campaign Brief template: a
branded cover with version and dates, numbered sections a marketer actually
reads (overview, audience, proposition, channels, measurement), an Insights
section grounded in the versioned brand playbook (buyer personas, the relevant
practice's approved proof points, buyer-journey guidance), a Notes section that
preserves human commentary with attribution, a provenance-and-approval record,
and a pre-launch quality checklist. Every populated field carries its source
(guardrail 1). Nothing about the campaign's substance is invented: the brief
distinguishes agent-provided facts from what marketing must still complete.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from shiftai_shared.brand import BrandRules
from shiftai_shared.m365.word import DocSection, DocSpec, build_docx

from campaign_identification import BRIEF_TEMPLATE_VERSION
from campaign_identification.models import (
    BcFoCheck,
    BriefField,
    CampaignBrief,
    CampaignRequest,
    Classification,
    ConflictFlag,
)

_SOURCE_LABEL = {
    "form": "intake form",
    "plan": "quarterly marketing plan",
    "calendar": "event calendar",
    "adhoc": "ad-hoc request",
}

_BRIEF_FIELD_ORDER = [
    "objective",
    "business_unit",
    "vertical",
    "target_segment",
    "offer_topic",
    "channels",
    "timeline_start",
    "timeline_end",
    "owner",
    "budget_flag",
    "requester",
]

_FIELD_LABEL = {
    "objective": "Objective",
    "business_unit": "Business unit",
    "vertical": "Vertical(s)",
    "target_segment": "Target segment(s)",
    "offer_topic": "Offer / topic",
    "channels": "Channels",
    "timeline_start": "Window start",
    "timeline_end": "Window end",
    "owner": "Campaign owner",
    "budget_flag": "Budget approved",
    "requester": "Requester",
}

_CHANNEL_ROLE = {
    "linkedin": "Reach and engage the audience in-feed; build awareness and social proof.",
    "email": "Nurture known contacts through the buyer journey with relevance and timing.",
    "events": "Convert Type 3/4 accounts; the proven channel for high-touch segments.",
    "web": "Answer 'can these people help me?' and move interest to a conversation.",
    "sales_enablement": "Equip sales and partners with context, talk track, and follow-up.",
    "paid_media": "Extend reach to net-new audiences against the campaign proposition.",
}


@dataclass(frozen=True)
class BriefNote:
    """One piece of human commentary on the brief, kept with attribution."""

    author: str
    context: str  # e.g. "Original request", "Revision directive"
    text: str


def _provenance(request: CampaignRequest, field: str, normalized: dict[str, str]) -> str:
    base = _SOURCE_LABEL[request.source]
    refs = f" ({'; '.join(request.source_refs)})" if request.source_refs else ""
    note = ", normalized by agent" if field in normalized else ""
    return f"{base}{refs}{note}"


def assemble_brief(
    *,
    case_id: str,
    request: CampaignRequest,
    classification: Classification | None,
    conflicts: list[ConflictFlag],
    bc_fo: BcFoCheck,
    normalized_fields: dict[str, str],
    version: int,
    campaign_id: str | None = None,
) -> CampaignBrief:
    fields: list[BriefField] = []
    for name in _BRIEF_FIELD_ORDER:
        raw_value = normalized_fields.get(name, getattr(request, name, None))
        if raw_value in (None, "", []):
            continue
        value = ", ".join(raw_value) if isinstance(raw_value, list) else str(raw_value)
        fields.append(
            BriefField(
                name=name, value=value, provenance=_provenance(request, name, normalized_fields)
            )
        )
    return CampaignBrief(
        campaign_id=campaign_id or f"cmp_{uuid.uuid4().hex[:10]}",
        case_id=case_id,
        version=version,
        status="awaiting_approval",
        fields=fields,
        classification=classification,
        conflicts=conflicts,
        bc_fo=bc_fo if bc_fo.mixed else None,
        template_version=BRIEF_TEMPLATE_VERSION,
        created_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
    )


def _fields_map(brief: CampaignBrief) -> dict[str, str]:
    return {f.name: f.value for f in brief.fields}


def _format_date(value: str) -> str:
    try:
        return date.fromisoformat(value[:10]).strftime("%d %B %Y")
    except ValueError:
        return value


def _human_pending(label: str) -> str:
    return f"[To complete: {label}]"


def _overview_section(fm: dict[str, str]) -> DocSection:
    rows = [
        ("Objective", fm.get("objective", _human_pending("business outcome, audience, action"))),
        ("Offer / topic", fm.get("offer_topic", _human_pending("the value exchange"))),
        ("Business unit", fm.get("business_unit", _human_pending("BU / practice"))),
        ("Budget approved", fm.get("budget_flag", _human_pending("yes / no / pending"))),
        ("Campaign owner", fm.get("owner", _human_pending("name and email"))),
        ("Requester", fm.get("requester", "")),
    ]
    return DocSection(
        number="01", heading="Campaign overview",
        intro="The business case, offer, and ownership at a glance.",
        table_header=("Field", "Value"),
        table_rows=tuple((k, v) for k, v in rows if v),
    )


def _audience_section(fm: dict[str, str]) -> DocSection:
    segment = fm.get("target_segment") or _human_pending("segment / tier / revenue band")
    paragraphs = [
        f"Target segment(s): {segment}",
        f"Priority industries: {fm.get('vertical') or _human_pending('approved industries')}",
        "Primary audience, business problem, and market trigger: to be completed by "
        "marketing with the account and buying-role detail that guides content.",
    ]
    return DocSection(
        number="02", heading="Audience and market context",
        intro="Ground the campaign in an identifiable enterprise audience and a specific reality.",
        paragraphs=tuple(paragraphs),
    )


def _relevant_practice(rules: BrandRules, fm: dict[str, str]) -> object | None:
    haystack = " ".join(
        fm.get(k, "") for k in ("business_unit", "offer_topic", "objective")
    ).lower()
    best = None
    best_score = 0
    for practice in rules.practices:
        score = sum(
            1 for token in (practice.id, *practice.name.lower().split())
            if len(token) > 3 and token in haystack
        )
        if score > best_score:
            best, best_score = practice, score
    return best if best_score > 0 else None


def _insights_section(brief: CampaignBrief, rules: BrandRules) -> DocSection:
    fm = _fields_map(brief)
    paragraphs: list[str] = [
        "Starter material from the LevelShift brand playbook. Use it to shape the "
        "proposition and messaging; confirm every claim against an approved source "
        "before it ships.",
    ]
    practice = _relevant_practice(rules, fm)
    if practice is not None:
        paragraphs.append(
            f"Relevant practice, {practice.name}: {practice.tagline} "  # type: ignore[attr-defined]
            f"Benefit, {practice.benefit} "  # type: ignore[attr-defined]
            f"Approved proof point, {practice.proof_point}"  # type: ignore[attr-defined]
        )
    persona_rows = tuple(
        (p.title, f"Pains: {p.pains} Key message: {p.key_message}") for p in rules.personas
    )
    return DocSection(
        number="03", heading="Proposition and messaging (brand insights)",
        intro="Buyer personas and approved positioning to build the proposition from.",
        paragraphs=tuple(paragraphs),
        table_header=("Buyer persona", "Pains and key message"),
        table_rows=persona_rows,
    )


def _channels_section(brief: CampaignBrief) -> DocSection:
    fm = _fields_map(brief)
    channels = [c.strip() for c in fm.get("channels", "").split(",") if c.strip()]
    default_role = "Define this channel's role in the journey."
    if channels:
        grid = tuple(
            (c, _CHANNEL_ROLE.get(c.lower().replace(" ", "_"), default_role))
            for c in channels
        )
    else:
        grid = (("[To complete]", "Select channels and define each one's role in the journey."),)
    return DocSection(
        number="04", heading="Channels, content, and activation",
        intro="How the campaign reaches the audience and moves them toward the call to action.",
        columns=("Channel", "Role in the journey"),
        grid=grid,
    )


def _measurement_section(fm: dict[str, str]) -> DocSection:
    start = fm.get("timeline_start")
    end = fm.get("timeline_end")
    period = (
        f"{_format_date(start)} to {_format_date(end)}" if start and end
        else _human_pending("campaign start and end dates")
    )
    return DocSection(
        number="05", heading="Measurement, budget, and delivery",
        intro="Agree success measures and dependencies before production begins.",
        table_header=("Field", "Value"),
        table_rows=(
            ("Campaign period", period),
            ("Budget approved", fm.get("budget_flag", _human_pending("yes / no / pending"))),
            ("Business goal", _human_pending("qualified pipeline / MQLs / meetings / revenue")),
            ("Primary success metric", _human_pending("metric, target, source, owner")),
            ("Dependencies and risks", _human_pending("approvals, SMEs, permissions, data")),
        ),
    )


def _provenance_section(brief: CampaignBrief) -> DocSection:
    fm = _fields_map(brief)
    prov = {f.name: f.provenance for f in brief.fields}
    grid = tuple(
        (_FIELD_LABEL.get(name, name), fm.get(name, "(not provided)"), prov.get(name, "(pending)"))
        for name in _BRIEF_FIELD_ORDER
        if name in fm
    )
    return DocSection(
        number="06", heading="Provenance and classification",
        intro="Where each decision came from. Missing information is flagged, never inferred.",
        columns=("Field / decision", "Approved value", "Source"),
        grid=grid,
    )


def _classification_section(c: Classification) -> DocSection:
    rationale = tuple(
        (f"Rationale: {field}", source) for field, source in sorted(c.field_rationale.items())
    )
    return DocSection(
        heading="Agent classification",
        intro="The agent's read of the request, tied to named source fields.",
        table_header=("Attribute", "Value"),
        table_rows=(
            ("Campaign type", c.campaign_type),
            ("Priority", c.priority),
            ("Channel mix", ", ".join(c.channel_mix)),
            ("Segment relevance", c.segment_relevance),
            *rationale,
        ),
    )


def _notes_section(notes: tuple[BriefNote, ...]) -> DocSection:
    return DocSection(
        heading="Notes and human commentary",
        intro="Human input on record for this brief, kept with attribution.",
        columns=("From", "Context", "Comment"),
        grid=tuple((n.author, n.context, n.text) for n in notes),
    )


def _approval_section(brief: CampaignBrief) -> DocSection:
    return DocSection(
        heading="Approval record",
        intro=(
            "This brief advances only on an explicit, recorded approval by the "
            "designated BU Campaign Lead. Status: awaiting approval."
        ),
        columns=("Approver / role", "Decision", "Date", "Comments"),
        grid=(
            ("[BU Campaign Lead]", "[Approve / revise]", "[YYYY-MM-DD]", "[Recorded on decision]"),
        ),
    )


def _quality_check_section(rules: BrandRules | None) -> DocSection:
    checks = rules.content_self_check if rules else (
        "The campaign leads with a business problem or insight, not a product description.",
        "The objective includes an audience, intended action, measurable outcome, and timeframe.",
        "Proof points and quantified claims have an approved source.",
        "The call to action is singular and specific.",
    )
    return DocSection(
        number="07", heading="Pre-launch quality check",
        intro="Confirm every item before activation.",
        checklist=tuple(checks),
    )


def brief_docx(
    brief: CampaignBrief,
    *,
    brand_rules: BrandRules | None = None,
    notes: tuple[BriefNote, ...] = (),
) -> bytes:
    """Materialize the brief as a Word document per the standard template."""
    fm = _fields_map(brief)
    campaign_name = fm.get("offer_topic") or fm.get("objective") or brief.campaign_id
    brief_date = _format_date(brief.created_at)
    campaign_type = brief.classification.campaign_type if brief.classification else "Campaign"

    sections: list[DocSection] = [
        _overview_section(fm),
        _audience_section(fm),
    ]
    if brand_rules is not None:
        sections.append(_insights_section(brief, brand_rules))
    sections.append(_channels_section(brief))
    sections.append(_measurement_section(fm))
    sections.append(_provenance_section(brief))
    if brief.classification is not None:
        sections.append(_classification_section(brief.classification))
    if notes:
        sections.append(_notes_section(notes))
    if brief.conflicts:
        sections.append(
            DocSection(
                heading="Duplicate / conflict flags (human decision required)",
                columns=("Conflicting campaign", "Kind", "Rationale"),
                grid=tuple(
                    (c.conflicting_campaign_id, f"{c.kind}, {c.freshness}", c.rationale)
                    for c in brief.conflicts
                ),
            )
        )
    if brief.bc_fo is not None and brief.bc_fo.mixed:
        sections.append(
            DocSection(
                heading="Business Central and F&O independence (split proposal)",
                intro="This request mixes Business Central and F&O; they must not be merged.",
                paragraphs=tuple(brief.bc_fo.split_proposal),
            )
        )
    sections.append(_approval_section(brief))
    sections.append(_quality_check_section(brand_rules))

    spec = DocSpec(
        kicker="Marketing campaign brief",
        title=campaign_name,
        subtitle=(
            f"{fm.get('business_unit', 'Business unit pending')}  |  {campaign_type}  |  "
            f"Brief v{brief.version}  |  {brief_date}"
        ),
        meta=(
            ("Campaign ID", brief.campaign_id),
            ("Case", brief.case_id),
            ("Brief version", f"v{brief.version}"),
            ("Brief date", brief_date),
            ("Prepared by", "Campaign Identification Agent, verified by the requester"),
            ("Template", brief.template_version),
        ),
        sections=tuple(sections),
    )
    return build_docx(spec)


def brief_filename(brief: CampaignBrief) -> str:
    return f"{brief.campaign_id}-brief-v{brief.version}.docx"
