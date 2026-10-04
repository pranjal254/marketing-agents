"""Read-only tools the studio assistant may call.

This is the whole of what the assistant can reach. The registry is a allowlist,
not a filter over a larger surface: a capability absent from here cannot be
invoked, however the model phrases its request.

Three rules hold for every tool.

*Read-only.* Nothing here writes, and nothing calls an agent. Every
gate-advancing path in the bridge is identity-stamped and sequenced; a chat turn
is neither, so letting the assistant drive one would route around the human
gates the agents are built on. Tools read the context store and nothing else.

*Trimmed on purpose.* Each tool returns a projection shaped for a reader, not
the raw record. A plan detail payload is tens of kilobytes of candidate scores
and document refs, almost none of which helps answer a question, and all of
which crowds out the part that does. Draft prose is behind its own tool so it
is fetched deliberately rather than swept up by a broad call.

*Defensive about shape.* Projections read dumped records with ``.get`` rather
than attribute access. A renamed field should degrade one line of an answer,
never crash the assistant mid-conversation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from c2c_campaign_box import persistence as box_db
from c2c_collaboration import persistence as collab_db
from c2c_content_repurposing import persistence as rp_db
from c2c_quality_gate import persistence as gate_db
from campaign_identification import persistence as intake_db
from shiftai_shared.context_store.store import ContextStore

MAX_LIST = 50
MAX_TEXT_CHARS = 6_000


class ToolError(Exception):
    """A tool could not answer. Reported back to the model, never raised to HTTP:
    a missing campaign is something the assistant should say, not a 500."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: str
    run: Callable[..., Any]


def _dump(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record
    dump = getattr(record, "model_dump", None)
    return dict(dump()) if callable(dump) else {}


def _pick(record: Any, *names: str) -> dict[str, Any]:
    data = _dump(record)
    return {n: data.get(n) for n in names if data.get(n) is not None}


def _clip(text: Any, limit: int = MAX_TEXT_CHARS) -> str:
    body = str(text or "")
    return body if len(body) <= limit else body[:limit] + "\n…[truncated]"


def _latest(store: ContextStore, kind: str, key: str) -> dict[str, Any]:
    """The newest version of one record, or an empty dict. Absence is normal
    here: a campaign mid-journey simply has no manifest yet."""
    record = store.get(kind, key)
    return _dump(record.value) if record is not None else {}

# --------------------------------------------------------------- campaigns


def list_campaigns(store: ContextStore) -> dict[str, Any]:
    """Every campaign the agents know about, newest records last."""
    rows = []
    for record in store.query(box_db.KIND_PLAN_CASE)[:MAX_LIST]:
        case = _dump(record.value)
        rows.append(
            {
                "campaign_id": record.key,
                "status": case.get("status"),
                "campaign_slug": case.get("campaign_slug"),
                "folder": case.get("folder"),
                "updated_at": case.get("updated_at"),
                "pack_confirmed": bool(case.get("confirmations", {}).get("pack")),
                "plan_confirmed": bool(case.get("confirmations", {}).get("plan")),
            }
        )
    return {"campaigns": rows, "count": len(rows)}


def get_campaign(store: ContextStore, campaign_id: str) -> dict[str, Any]:
    """Plan state, the audience and offer pack, the asset checklist, the
    schedule and the package manifest if one exists."""
    case = box_db.load_plan_case(store, campaign_id)
    if case is None:
        raise ToolError(f"no campaign {campaign_id!r}. Use list_campaigns to see what exists.")

    pack = _latest(store, box_db.KIND_PACK, campaign_id)
    checklist = _latest(store, box_db.KIND_CHECKLIST, campaign_id)
    plan = _latest(store, box_db.KIND_WORKFLOW_PLAN, campaign_id)
    manifest = _latest(store, box_db.KIND_MANIFEST, campaign_id)

    return {
        "campaign_id": campaign_id,
        "status": case.get("status"),
        "campaign_slug": case.get("campaign_slug"),
        "confirmations": case.get("confirmations", {"pack": False, "plan": False}),
        "audience_offer_pack": {
            "value_proposition": pack.get("value_proposition"),
            "differentiators": pack.get("differentiators"),
            "personas": [
                _pick(p, "persona_id", "title", "role_pains") for p in pack.get("personas", [])
            ],
            "proof_points": [
                _pick(p, "claim", "source_ref", "status") for p in pack.get("proof_points", [])
            ],
            "messaging_angles": pack.get("messaging_angles"),
            "channel_emphasis": pack.get("channel_emphasis"),
            "gaps": pack.get("gaps"),
            "intel_mode": pack.get("intel_mode"),
        },
        "asset_checklist": [
            {
                **_pick(i, "asset_id", "asset_type", "label", "volume", "decision", "status"),
                "rationale": _clip(_dump(i).get("decision_rationale"), 300),
            }
            for i in checklist.get("items", [])
        ],
        "schedule": [
            _pick(m, "asset_id", "milestone", "due_date") for m in plan.get("milestones", [])
        ][:MAX_LIST],
        "feasible": plan.get("feasible"),
        "package_manifest": _pick(manifest, "version", "created_at", "status")
        if manifest
        else None,
        "packaged_assets": [
            _pick(a, "asset_id", "filename", "sha256") for a in manifest.get("assets", [])
        ],
    }


# ------------------------------------------------------------------ drafts


def get_drafts(store: ContextStore, campaign_id: str) -> dict[str, Any]:
    """Which assets have been written, their status and quality flags. Prose is
    NOT included: call get_draft_text for one asset when the wording matters."""
    case = rp_db.load_case(store, campaign_id)
    drafts = rp_db.load_drafts(store, campaign_id)
    if case is None and not drafts:
        raise ToolError(f"no drafting has started for {campaign_id!r}")

    rows = []
    for draft in drafts:
        data = _dump(draft)
        check = data.get("self_check") or {}
        rows.append(
            {
                **_pick(draft, "asset_id", "asset_type", "kind", "title", "version", "status"),
                "sections": len(data.get("sections") or []),
                "self_check": {
                    "passed": check.get("passed"),
                    "attempts": check.get("attempts"),
                    "word_count": check.get("word_count"),
                    "word_range": check.get("word_range"),
                    "word_count_in_range": check.get("word_count_in_range"),
                    "findings": [
                        _pick(f, "rule_id", "severity", "term") for f in check.get("findings", [])
                    ],
                    "unsourced_numeric_tokens": check.get("unsourced_numeric_tokens"),
                },
                "rework_of_version": data.get("rework_of_version"),
            }
        )
    return {
        "campaign_id": campaign_id,
        "status": (case or {}).get("status"),
        "drafts": rows,
        "gap_notes": [
            _pick(g, "asset_id", "section", "needed")
            for g in rp_db.load_gap_notes(store, campaign_id)
        ],
    }


def get_draft_text(store: ContextStore, campaign_id: str, asset_id: str) -> dict[str, Any]:
    """The actual prose of the latest version of one asset."""
    draft = rp_db.latest_draft(store, campaign_id, asset_id)
    if draft is None:
        raise ToolError(
            f"no draft for asset {asset_id!r} on {campaign_id!r}. "
            "Use get_drafts to see which assets exist."
        )
    data = _dump(draft)
    body = "\n\n".join(
        f"## {s.get('heading', '')}\n" + "\n\n".join(s.get("paragraphs") or [])
        for s in data.get("sections") or []
    )
    return {
        "campaign_id": campaign_id,
        "asset_id": asset_id,
        "title": data.get("title"),
        "version": data.get("version"),
        "status": data.get("status"),
        "text": _clip(body),
        "claim_markers": [
            _pick(m, "marker", "claim", "source_ref") for m in data.get("claim_markers") or []
        ],
    }


# ------------------------------------------------------------------ review


def get_review(store: ContextStore, campaign_id: str) -> dict[str, Any]:
    """Reviewer feedback, review rounds and unresolved conflicts per asset."""
    states = collab_db.load_states(store, campaign_id)
    if not states:
        raise ToolError(f"no review activity for {campaign_id!r} yet")
    assets = []
    for state in states:
        data = _dump(state)
        asset_id = str(data.get("asset_id", ""))
        assets.append(
            {
                **_pick(state, "asset_id", "status", "round", "confirmed_by", "confirmed_at"),
                "open_feedback": [
                    {
                        **_pick(f, "feedback_id", "reviewer_role", "section", "status"),
                        "text": _clip(_dump(f).get("text"), 400),
                    }
                    for f in collab_db.open_feedback(store, campaign_id, asset_id)
                ],
                "conflicts": [
                    {
                        **_pick(c, "conflict_id", "status"),
                        "summary": _clip(_dump(c).get("summary") or _dump(c).get("detail"), 300),
                    }
                    for c in collab_db.load_conflicts(store, campaign_id, asset_id)
                ],
            }
        )
    return {"campaign_id": campaign_id, "assets": assets}


# -------------------------------------------------------------------- gate


def get_gate(store: ContextStore, campaign_id: str) -> dict[str, Any]:
    """Compliance findings per asset, the open review tasks and who holds them,
    and the approval chain."""
    state = gate_db.load_gate_state(store, campaign_id)
    reports = gate_db.load_reports(store, campaign_id)
    tasks = gate_db.load_tasks(store, campaign_id)
    if state is None and not reports and not tasks:
        raise ToolError(f"the quality gate has not run for {campaign_id!r}")
    return {
        "campaign_id": campaign_id,
        "gate_status": _dump(state).get("status"),
        "reports": [
            {
                **_pick(r, "asset_id", "verdict", "risk"),
                "findings": [
                    {
                        **_pick(f, "rule_id", "severity", "location"),
                        "detail": _clip(_dump(f).get("detail"), 300),
                        "quote": _clip(_dump(f).get("quote"), 200),
                    }
                    for f in _dump(r).get("findings", [])
                ],
            }
            for r in reports
        ],
        "tasks": [
            _pick(t, "task_id", "scope", "asset_id", "status", "assignee_role", "due_date")
            for t in tasks
        ],
        "approvals": [
            _pick(a, "scope", "asset_id", "actor_id", "actor_role", "decision", "at")
            for a in gate_db.load_approvals(store, campaign_id)
        ],
        "locks": [_pick(lock, "asset_id", "sha256") for lock in _dump(state).get("locks", [])],
    }


# -------------------------------------------------------- content settings


def get_content_settings(store: ContextStore, campaign_id: str) -> dict[str, Any]:
    """How many of each asset this campaign asks for and the word range per one."""
    settings = rp_db.load_content_settings(store, campaign_id)
    if settings is None:
        return {
            "campaign_id": campaign_id,
            "saved": False,
            "note": "no settings saved; the plan volumes and the config word ranges apply",
        }
    data = _dump(settings)
    return {
        "campaign_id": campaign_id,
        "saved": True,
        "version": data.get("version"),
        "set_by": data.get("set_by"),
        "set_at": data.get("set_at"),
        "items": [
            _pick(i, "asset_id", "label", "variants", "min_words", "max_words")
            for i in data.get("items", [])
        ],
        "adjustments": data.get("adjustments"),
    }


# ------------------------------------------------------------------ intake


def get_intake_case(store: ContextStore, case_id: str) -> dict[str, Any]:
    """The original request behind a campaign: brief fields, classification,
    gap questions and the approval decision."""
    case = intake_db.load_case(store, case_id)
    if case is None:
        raise ToolError(f"no intake case {case_id!r}")
    brief = _dump(case.get("brief"))
    return {
        "case_id": case_id,
        "status": case.get("status"),
        "campaign_id": brief.get("campaign_id") or case.get("campaign_id"),
        "brief_fields": [
            _pick(f, "name", "value", "provenance") for f in brief.get("fields", [])
        ],
        "classification": brief.get("classification"),
        "escalation_reason": case.get("escalation_reason"),
        "decision": case.get("decision"),
    }


def list_intake_cases(store: ContextStore) -> dict[str, Any]:
    """Requests in flight, including those still waiting on the requester."""
    rows = []
    for record in store.query(intake_db.KIND_CASE)[:MAX_LIST]:
        case = _dump(record.value)
        brief = _dump(case.get("brief"))
        rows.append(
            {
                "case_id": record.key,
                "status": case.get("status"),
                "campaign_id": brief.get("campaign_id"),
                "requester": case.get("requester"),
                "updated_at": case.get("updated_at"),
            }
        )
    return {"cases": rows, "count": len(rows)}


# --------------------------------------------------------------- telemetry


def get_telemetry_summary(store: ContextStore) -> dict[str, Any]:
    """The banked aggregate rollup: cost, volume and latency by journey step and
    by agent. Figures come from the last export refresh, not live."""
    from shiftai_shared.process import PROCESS_PACK

    from c2c_bridge.telemetry_routes import KIND_TELEMETRY_SNAPSHOT

    record = store.get(KIND_TELEMETRY_SNAPSHOT, PROCESS_PACK.process_name)
    if record is None:
        raise ToolError(
            "no telemetry snapshot has been taken yet; ask an operator to refresh the export"
        )
    snapshot = _dump(record.value)
    return {
        "generated_at": snapshot.get("generated_at"),
        "window": snapshot.get("window"),
        "totals": snapshot.get("totals"),
        "by_stage": snapshot.get("by_stage"),
        "by_agent": snapshot.get("by_agent"),
        "by_model": snapshot.get("by_model"),
    }


# ---------------------------------------------------------------- registry

TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "list_campaigns",
            "Every campaign the agents know about, with its planning status. Start here "
            "when the question names no campaign.",
            "{}",
            list_campaigns,
        ),
        Tool(
            "get_campaign",
            "One campaign in depth: audience and offer pack, asset checklist with "
            "reuse/adapt/create decisions, schedule, and the package manifest.",
            '{"campaign_id": string}',
            get_campaign,
        ),
        Tool(
            "get_drafts",
            "Which assets have been written for a campaign, their status, word counts "
            "and quality flags. Does not include the prose.",
            '{"campaign_id": string}',
            get_drafts,
        ),
        Tool(
            "get_draft_text",
            "The actual written content of one asset. Use only when the wording itself "
            "is the question; it is long.",
            '{"campaign_id": string, "asset_id": string}',
            get_draft_text,
        ),
        Tool(
            "get_review",
            "Reviewer feedback, review rounds and unresolved conflicts per asset.",
            '{"campaign_id": string}',
            get_review,
        ),
        Tool(
            "get_gate",
            "Quality gate results: compliance findings per asset, open review tasks and "
            "who holds them, the approval chain and content locks.",
            '{"campaign_id": string}',
            get_gate,
        ),
        Tool(
            "get_content_settings",
            "How many variants of each asset this campaign asks for and the word range "
            "for each, plus who set them.",
            '{"campaign_id": string}',
            get_content_settings,
        ),
        Tool(
            "list_intake_cases",
            "Campaign requests in flight, including ones still waiting on the requester.",
            "{}",
            list_intake_cases,
        ),
        Tool(
            "get_intake_case",
            "The original request behind a campaign: brief fields with provenance, "
            "classification and the approval decision.",
            '{"case_id": string}',
            get_intake_case,
        ),
        Tool(
            "get_telemetry_summary",
            "Aggregated cost, volume and latency by journey step and by agent, from the "
            "last export refresh.",
            "{}",
            get_telemetry_summary,
        ),
    )
}


def catalog() -> str:
    """The toolset as the model sees it."""
    return "\n".join(
        f"- {t.name}{t.args}: {t.description}" for t in TOOLS.values()
    )


def invoke(store: ContextStore, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool by name. An unknown name is a model mistake, not a crash:
    it comes back as a result the model can read and correct from."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(
            f"no tool called {name!r}. Available: {', '.join(sorted(TOOLS))}"
        )
    clean = {k: v for k, v in (args or {}).items() if v is not None}
    try:
        result = tool.run(store, **clean)
    except ToolError:
        raise
    except TypeError as exc:
        raise ToolError(f"{name} called with wrong arguments ({exc}); expects {tool.args}") from exc
    return result if isinstance(result, dict) else {"result": result}
