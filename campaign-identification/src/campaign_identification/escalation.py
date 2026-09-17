"""Human-readable escalation packages (Task: escalations are never dead ends).

Every escalation the agent raises gets a plain-language explanation built here —
what it found, why the rule exists, who decides, and the concrete ways to move
forward. The studio renders these verbatim; resolution options are machine-
actionable (each carries the field patch a resume would apply), so the person
the case routed to can resolve it in one click instead of decoding a reason code.
"""

from __future__ import annotations

from typing import Any

# Queue id (config routingMap) -> the human label the studio shows.
QUEUE_LABELS: dict[str, str] = {
    "marketing-lead-queue": "Marketing Lead",
    "bu-campaign-lead-queue": "BU Campaign Lead",
    "requester": "the requester",
    "aicoe-queue": "AiCoE Admin",
}


def _option(
    option_id: str,
    label: str,
    kind: str,
    *,
    patch: dict[str, str] | None = None,
    roles: list[str] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "id": option_id,
        "label": label,
        # "resolve": one click patches fields and resumes the same case.
        # "edit": reopen the form pre-filled. "restart": new request.
        "kind": kind,
        "patch": patch or {},
        "roles": roles or [],  # empty = anyone; else only these roles may click
        "note": note,
    }


def explain_escalation(
    reason_code: str,
    detail: dict[str, Any],
    routed_to: str,
) -> dict[str, Any]:
    """One package per escalation: title, why, the policy in one line, and options."""
    routed_role = QUEUE_LABELS.get(routed_to, routed_to)
    base: dict[str, Any] = {
        "reason_code": reason_code,
        "routed_to": routed_to,
        "routed_to_role": routed_role,
        "evidence": [],
        "options": [],
    }

    if reason_code == "bc_fo_mix":
        evidence = sorted({str(e) for e in detail.get("evidence", [])})
        return {
            **base,
            "title": "This brief mentions both Business Central and F&O",
            "why": (
                "The agent found both product lines in the request "
                f"({', '.join(evidence)}) without an explicit single scope, so it "
                "cannot tell which one this campaign is for."
            ),
            "policy": (
                "LevelShift policy: one campaign targets one product line — BC and "
                "F&O are never blended. The agent proposes, a human decides."
            ),
            "evidence": evidence,
            "options": [
                _option(
                    "scope_bc",
                    "Scope it to Business Central only",
                    "resolve",
                    patch={"products": "BC"},
                    note="F&O mentions stay as audience context on the brief.",
                ),
                _option(
                    "scope_fo",
                    "Scope it to F&O only",
                    "resolve",
                    patch={"products": "FO"},
                    note="BC mentions stay as audience context on the brief.",
                ),
                _option("edit", "Edit the brief myself", "edit"),
            ],
        }

    if reason_code == "compliance_ceiling":
        terms = sorted({str(t) for t in detail.get("matched_terms", [])})
        return {
            **base,
            "title": "This brief touches pricing, legal, or partner-commitment territory",
            "why": (
                f"The agent spotted the term{'s' if len(terms) != 1 else ''} "
                f"{', '.join(repr(t) for t in terms)} in the request. It is not "
                "allowed to decide alone whether a campaign makes a pricing, legal, "
                "or partner commitment — that call is always a person's."
            ),
            "policy": (
                "Authority envelope: anything touching pricing, legal, or partner "
                "commitments needs a named human to confirm before the brief moves on."
            ),
            "evidence": terms,
            "options": [
                _option(
                    "confirm_no_commitment",
                    "Confirm: no pricing, legal, or partner commitment is made — continue",
                    "resolve",
                    patch={},  # the studio fills compliance_ack with the confirmer's identity
                    roles=["Marketing Lead", "AiCoE Admin"],
                    note="Your name and role are recorded on the case as the confirmation.",
                ),
                _option("edit", "Edit the brief to remove the sensitive wording", "edit"),
            ],
        }

    if reason_code == "duplicate_disputed":
        ids = [str(i) for i in detail.get("conflicting_campaign_ids", [])]
        return {
            **base,
            "title": "A very similar campaign is already open",
            "why": (
                "An open campaign with the same business unit, vertical, and an "
                f"overlapping topic and window is on the calendar ({', '.join(ids)}). "
                "Running both as-is would compete for the same audience."
            ),
            "policy": (
                "Fresh duplicates are flagged, never silently merged or dropped — "
                "a human decides whether to adjust or proceed."
            ),
            "evidence": ids,
            "options": [
                _option(
                    "edit",
                    "Adjust the topic, audience, or dates and resubmit",
                    "edit",
                ),
                _option("restart", "Start a different request", "restart"),
            ],
        }

    if reason_code in ("unclassifiable_bu", "low_confidence"):
        return {
            **base,
            "title": "The agent could not confidently classify this request",
            "why": str(
                detail.get("rationale")
                or "It could not map the request to a business unit and campaign type "
                "with enough confidence, and it never guesses."
            ),
            "policy": "Below the confidence bar the agent abstains and asks a human.",
            "options": [
                _option(
                    "edit",
                    "Add detail (business unit, product, audience) and resubmit",
                    "edit",
                ),
                _option("restart", "Start over with a clearer description", "restart"),
            ],
        }

    if reason_code in ("tool_failure", "sla_breach"):
        return {
            **base,
            "title": "The agent is paused by its safety controls",
            "why": str(
                detail.get("control_pause_reason")
                or "A control-plane guard (kill switch or rate breaker) paused "
                "automatic actions."
            ),
            "policy": "While paused, nothing executes automatically — an AiCoE admin resumes it.",
            "options": [_option("restart", "Try again once the pause is lifted", "restart")],
        }

    # Unknown/rare codes still get a humane default instead of a bare slug.
    return {
        **base,
        "title": "The agent needs a human decision on this request",
        "why": f"It stopped on '{reason_code}' rather than act on uncertainty.",
        "policy": "When unsure, the agent escalates instead of guessing.",
        "options": [
            _option("edit", "Review and adjust the request", "edit"),
            _option("restart", "Start a new request", "restart"),
        ],
    }
