"""Task 8 — the BU Campaign Lead approval gate, plus Standard-C learning-signal
helpers.

The gate is structural: nothing in this agent can set a brief to approved except
``record`` receiving an explicit human decision with a non-empty actor identity.
The agent routes and waits — it never approves (guardrail 2).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from campaign_identification.models import ApprovalRecord, CampaignBrief


class ApprovalGateError(Exception):
    """Raised when a human decision is malformed (missing identity) or out of order."""


def record(
    *,
    decision: str,
    actor_role: str,
    actor_id: str,
    notes: str | None = None,
) -> ApprovalRecord:
    """Validate and materialize a human decision. Identity + timestamp are mandatory
    (spec guardrail 2: approval recorded with identity and timestamp)."""
    if decision not in ("approved", "rejected", "modified"):
        raise ApprovalGateError(f"invalid decision {decision!r}")
    if not actor_role.strip() or not actor_id.strip():
        raise ApprovalGateError("human decision requires actor_role and actor_id")
    return ApprovalRecord(
        decision=decision,  # type: ignore[arg-type]
        actor_role=actor_role.strip(),
        actor_id=actor_id.strip(),
        timestamp=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        notes=notes,
    )


def scenario_hash(brief: CampaignBrief | None, action_class: str | None) -> str:
    """Standard C/D scenario identity: stable hash of the decision situation."""
    if brief is None:
        basis = f"none|none|{action_class}"
    else:
        fields = {f.name: f.value for f in brief.fields}
        flags = ",".join(sorted({c.kind for c in brief.conflicts}))
        basis = (
            f"{fields.get('business_unit', '')}|{fields.get('vertical', '')}|{action_class}|{flags}"
        )
    return hashlib.sha256(basis.lower().encode("utf-8")).hexdigest()[:16]


def learning_label(agent_recommendation: str | None, human_decision: str) -> str:
    """Standard C: the delta between agent recommendation and human action is the
    training signal."""
    if human_decision == "approved":
        return "correct"
    if human_decision == "rejected":
        return "false_positive"
    return "correct"  # modified: direction captured by the recommendation/action delta
