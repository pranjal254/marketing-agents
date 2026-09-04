"""Context Store persistence for Agent 4 — versioned, append-only records
(review state, feedback items, rounds, conflicts, iteration metrics).
Every kind is registered in the governance catalog (migration 0003)."""

from __future__ import annotations

from datetime import UTC, datetime

from shiftai_shared.context_store.store import ContextStore

from c2c_collaboration.models import (
    ConflictRecord,
    FeedbackItem,
    IterationMetrics,
    ReviewRound,
    ReviewState,
)

KIND_REVIEW_STATE = "review_assignment"
KIND_FEEDBACK = "feedback_item"
KIND_ROUND = "review_round"
KIND_CONFLICT = "conflict_record"
KIND_METRICS = "iteration_metrics"


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def save_state(store: ContextStore, state: ReviewState) -> None:
    store.put(KIND_REVIEW_STATE, f"{state.campaign_id}:{state.asset_id}", state.model_dump())


def load_state(store: ContextStore, campaign_id: str, asset_id: str) -> ReviewState | None:
    record = store.get(KIND_REVIEW_STATE, f"{campaign_id}:{asset_id}")
    return ReviewState.model_validate(record.value) if record else None


def load_states(store: ContextStore, campaign_id: str) -> list[ReviewState]:
    out: list[ReviewState] = []
    for record in store.query(KIND_REVIEW_STATE):
        if record.key.startswith(f"{campaign_id}:"):
            out.append(ReviewState.model_validate(record.value))
    out.sort(key=lambda s: s.asset_id)
    return out


def all_states(store: ContextStore) -> list[ReviewState]:
    return [ReviewState.model_validate(r.value) for r in store.query(KIND_REVIEW_STATE)]


def save_feedback(store: ContextStore, item: FeedbackItem) -> None:
    store.put(
        KIND_FEEDBACK,
        f"{item.campaign_id}:{item.asset_id}:{item.feedback_id}",
        item.model_dump(),
    )


def open_feedback(store: ContextStore, campaign_id: str, asset_id: str) -> list[FeedbackItem]:
    out: list[FeedbackItem] = []
    for record in store.query(KIND_FEEDBACK):
        if record.key.startswith(f"{campaign_id}:{asset_id}:"):
            item = FeedbackItem.model_validate(record.value)
            if item.status == "open":
                out.append(item)
    out.sort(key=lambda i: i.feedback_id)
    return out


def all_feedback(store: ContextStore, campaign_id: str, asset_id: str) -> list[FeedbackItem]:
    out: list[FeedbackItem] = []
    for record in store.query(KIND_FEEDBACK):
        if record.key.startswith(f"{campaign_id}:{asset_id}:"):
            out.append(FeedbackItem.model_validate(record.value))
    out.sort(key=lambda i: i.feedback_id)
    return out


def save_round(store: ContextStore, round_: ReviewRound) -> None:
    store.put(
        KIND_ROUND,
        f"{round_.campaign_id}:{round_.asset_id}:r{round_.round}",
        round_.model_dump(),
    )


def load_rounds(store: ContextStore, campaign_id: str, asset_id: str) -> list[ReviewRound]:
    out: list[ReviewRound] = []
    for record in store.query(KIND_ROUND):
        if record.key.startswith(f"{campaign_id}:{asset_id}:"):
            out.append(ReviewRound.model_validate(record.value))
    out.sort(key=lambda r: r.round)
    return out


def save_conflict(store: ContextStore, conflict: ConflictRecord) -> None:
    store.put(
        KIND_CONFLICT,
        f"{conflict.campaign_id}:{conflict.asset_id}:{conflict.conflict_id}",
        conflict.model_dump(),
    )


def load_conflicts(store: ContextStore, campaign_id: str, asset_id: str) -> list[ConflictRecord]:
    out: list[ConflictRecord] = []
    for record in store.query(KIND_CONFLICT):
        if record.key.startswith(f"{campaign_id}:{asset_id}:"):
            out.append(ConflictRecord.model_validate(record.value))
    out.sort(key=lambda c: c.conflict_id)
    return out


def load_conflict(
    store: ContextStore, campaign_id: str, asset_id: str, conflict_id: str
) -> ConflictRecord | None:
    record = store.get(KIND_CONFLICT, f"{campaign_id}:{asset_id}:{conflict_id}")
    return ConflictRecord.model_validate(record.value) if record else None


def save_metrics(store: ContextStore, metrics: IterationMetrics) -> None:
    store.put(
        KIND_METRICS, f"{metrics.campaign_id}:{metrics.asset_id}", metrics.model_dump()
    )


def now_iso() -> str:
    return _now()
