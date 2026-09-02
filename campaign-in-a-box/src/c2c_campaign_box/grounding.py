"""Never-invent enforcement for the planning pass (guardrail 1, in code).

The model proposes; this module verifies. A proof point whose ``source_ref`` does
not resolve to a gathered intel signal URI or an approved-brief field is marked
``unverified``, excluded from usable proof points, and recorded as an explicit gap
— never published, never silently dropped. Reuse decisions may only reference
evaluated candidates; ``create`` without a performed search becomes
``reuse_check_pending``. Brand language rules are linted deterministically.
"""

from __future__ import annotations

from shiftai_shared.brand import BrandRules, lint_text

from c2c_campaign_box.intake import ApprovedBrief
from c2c_campaign_box.models import (
    AssetChecklistItem,
    ContentOutline,
    IntelBundle,
    PackLLMOutput,
    ProofPoint,
    RepoCandidate,
    ReuseOutlineItem,
)


def valid_source_refs(bundle: IntelBundle, brief: ApprovedBrief) -> set[str]:
    refs = {s.source_uri for s in bundle.signals}
    refs |= {s.signal_id for s in bundle.signals}
    refs |= {f"brief:{name}" for name in brief.fields}
    return refs


def ground_proof_points(
    proof_points: list[ProofPoint], bundle: IntelBundle, brief: ApprovedBrief
) -> tuple[list[ProofPoint], list[ProofPoint], float]:
    """Split into (verified, excluded_unverified, unverified_share)."""
    refs = valid_source_refs(bundle, brief)
    verified: list[ProofPoint] = []
    unverified: list[ProofPoint] = []
    for pp in proof_points:
        if pp.source_ref in refs and pp.status == "verified":
            verified.append(pp)
        else:
            unverified.append(pp.model_copy(update={"status": "unverified"}))
    total = len(proof_points)
    share = (len(unverified) / total) if total else 0.0
    return verified, unverified, round(share, 4)


def ground_pack(
    output: PackLLMOutput,
    bundle: IntelBundle,
    brief: ApprovedBrief,
    rules: BrandRules,
) -> tuple[PackLLMOutput, list[ProofPoint], float, list[dict[str, str]]]:
    """Returns (grounded pack output, excluded proof points, unverified share,
    lint findings over all pack language)."""
    verified, excluded, share = ground_proof_points(output.proof_points, bundle, brief)
    gaps = list(output.gaps)
    for pp in excluded:
        gaps.append(f"unsourced claim excluded: {pp.claim[:120]} (ref: {pp.source_ref or 'none'})")
    grounded = output.model_copy(update={"proof_points": verified, "gaps": gaps})

    pack_text = " ".join(
        [
            grounded.value_proposition,
            *grounded.differentiators,
            *[p.claim for p in grounded.proof_points],
            *[a.angle for a in grounded.messaging_angles],
            *grounded.ctas.values(),
        ]
    )
    findings = [
        {"rule_id": f.rule_id, "severity": f.severity, "term": f.term, "detail": f.detail}
        for f in lint_text(pack_text, rules)
    ]
    return grounded, excluded, share, findings


def ground_reuse_items(
    items: list[ReuseOutlineItem],
    candidates_by_type: dict[str, list[RepoCandidate]],
    checklist_items: list[AssetChecklistItem],
    *,
    search_performed: bool,
    verified_refs: set[str],
) -> list[AssetChecklistItem]:
    """Merge the model's decisions into the deterministic checklist, enforcing:
    - decision must be for a known checklist asset (unknown items dropped);
    - reuse/adapt must cite an evaluated candidate ref — otherwise demoted to
      create with the violation recorded in the rationale;
    - no search performed → every decision becomes create + reuse_check_pending.
    """
    by_asset = {i.asset_id: i for i in items}
    grounded: list[AssetChecklistItem] = []
    for base in checklist_items:
        model_item = by_asset.get(base.asset_id)
        candidate_refs = {c.asset_ref for c in base.candidates_evaluated}
        if not search_performed:
            grounded.append(
                base.model_copy(
                    update={
                        "decision": "create",
                        "reuse_check_pending": True,
                        "reuse_ref": None,
                        "decision_rationale": (
                            "repository search unavailable — created pending reuse check "
                            "(spec fallback; never skip the reuse pass silently)"
                        ),
                    }
                )
            )
            continue
        if model_item is None:
            grounded.append(base)
            continue
        decision = model_item.decision
        reuse_ref = model_item.reuse_ref
        rationale = model_item.rationale.strip() or base.decision_rationale
        if decision in ("reuse", "adapt"):
            if reuse_ref not in candidate_refs:
                decision, reuse_ref = "create", None
                rationale = (
                    "model cited an asset outside the evaluated candidates — demoted to "
                    f"create (never-invent enforcement). Original rationale: {rationale}"
                )
        else:
            reuse_ref = None
        grounded.append(
            base.model_copy(
                update={
                    "decision": decision,
                    "reuse_ref": reuse_ref,
                    "decision_rationale": rationale,
                }
            )
        )
    return grounded


def ground_outlines(
    items: list[ReuseOutlineItem],
    checklist: list[AssetChecklistItem],
    verified_refs: set[str],
) -> list[ContentOutline]:
    """Outlines exist only for create/adapt assets; planned claims may only cite
    verified refs — anything else is stripped (a claim the pack cannot source is a
    gap note, never plausible content)."""
    decisions = {c.asset_id: c.decision for c in checklist}
    outlines: list[ContentOutline] = []
    for item in items:
        if item.outline is None or decisions.get(item.asset_id) not in ("create", "adapt"):
            continue
        sections = [
            s.model_copy(
                update={"planned_claims": [r for r in s.planned_claims if r in verified_refs]}
            )
            for s in item.outline.sections
        ]
        outlines.append(item.outline.model_copy(update={"sections": sections}))
    return outlines
