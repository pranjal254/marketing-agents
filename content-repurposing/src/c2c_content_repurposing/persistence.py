"""Context Store persistence for Agent 3 — versioned, append-only records
(repurpose case, staged drafts with lineage, claim inventory, gap notes, failures)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shiftai_shared.context_store.store import ContextStore

from c2c_content_repurposing.models import ClaimInventory, GapNote, StagedDraft

KIND_REPURPOSE_CASE = "repurpose_case"
KIND_STAGED_DRAFT = "staged_draft"
KIND_CLAIM_INVENTORY = "claim_inventory"
KIND_GAP_NOTE = "content_gap_note"
KIND_FAILED_RUN = "failed_repurpose_run"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def save_case(store: ContextStore, campaign_id: str, state: dict[str, Any]) -> None:
    store.put(KIND_REPURPOSE_CASE, campaign_id, {**state, "updated_at": _now()})


def load_case(store: ContextStore, campaign_id: str) -> dict[str, Any] | None:
    record = store.get(KIND_REPURPOSE_CASE, campaign_id)
    return record.value if record else None


def save_draft(store: ContextStore, draft: StagedDraft) -> None:
    """Every version is its own record — drafts are additive, never overwritten."""
    store.put(
        KIND_STAGED_DRAFT,
        f"{draft.campaign_id}:{draft.asset_id}:v{draft.version}",
        draft.model_dump(),
    )


def load_drafts(store: ContextStore, campaign_id: str) -> list[StagedDraft]:
    out: list[StagedDraft] = []
    for record in store.query(KIND_STAGED_DRAFT):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(StagedDraft.model_validate(record.value))
    out.sort(key=lambda d: (d.asset_id, d.version))
    return out


def latest_draft(store: ContextStore, campaign_id: str, asset_id: str) -> StagedDraft | None:
    versions = [d for d in load_drafts(store, campaign_id) if d.asset_id == asset_id]
    return versions[-1] if versions else None


def save_inventory(store: ContextStore, inventory: ClaimInventory) -> None:
    store.put(
        KIND_CLAIM_INVENTORY,
        f"{inventory.campaign_id}:v{inventory.flagship_version}",
        inventory.model_dump(),
    )


def load_inventory(
    store: ContextStore, campaign_id: str, flagship_version: int
) -> ClaimInventory | None:
    record = store.get(KIND_CLAIM_INVENTORY, f"{campaign_id}:v{flagship_version}")
    return ClaimInventory.model_validate(record.value) if record else None


def save_gap_notes(store: ContextStore, campaign_id: str, notes: list[GapNote]) -> None:
    for note in notes:
        store.put(
            KIND_GAP_NOTE,
            f"{campaign_id}:{note.gap_id}",
            {**note.model_dump(), "campaign_id": campaign_id, "created_at": _now()},
        )


def load_gap_notes(store: ContextStore, campaign_id: str) -> list[GapNote]:
    out: list[GapNote] = []
    for record in store.query(KIND_GAP_NOTE):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(GapNote.model_validate(record.value))
    out.sort(key=lambda g: g.gap_id)
    return out


def save_failed_run(
    store: ContextStore, campaign_id: str, error_type: str, detail: str
) -> None:
    """A drafting/fan-out failure is persisted, never discarded."""
    store.put(
        KIND_FAILED_RUN,
        f"{campaign_id}:{_now()}",
        {"campaign_id": campaign_id, "error_type": error_type, "detail": detail},
    )
