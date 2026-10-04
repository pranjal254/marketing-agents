"""Durable assistant conversations.

Chats live in the context store so they survive a browser, a device and a
restart. The browser keeps its own copy for instant reopen, but that copy is a
cache: this is the record.

The store is append-only by design, which fits a transcript exactly. Each turn
appended writes a new version of the conversation, so the history of a
conversation is itself recoverable, and nothing a person was told can be
quietly rewritten afterwards.

Conversations are scoped to the actor who asked. That is a listing convention,
not an access control: anyone who can reach the bridge can read any id they
know. Real per-user scoping arrives with SSO, and the gap is deliberate rather
than overlooked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from shiftai_shared.context_store.store import ContextStore

KIND_CONVERSATION = "assistant_conversation"

MAX_TITLE_CHARS = 72
MAX_TURNS = 200
MAX_LIST = 50


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


class ToolCallRecord(BaseModel):
    """One tool the assistant reached for while answering, kept so a person can
    see what an answer was actually based on."""

    model_config = ConfigDict(frozen=True)

    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    error: str | None = None
    duration_ms: int = 0


class Turn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str  # "you" | "assistant"
    text: str
    at: str = ""
    references: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    suggested_screen: str | None = None
    grounded: bool = True
    cost_usd: float | None = None
    model: str | None = None


class Conversation(BaseModel):
    model_config = ConfigDict(frozen=True)

    conversation_id: str
    actor_id: str = ""
    title: str = "New conversation"
    created_at: str = ""
    updated_at: str = ""
    turns: list[Turn] = Field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """List-row shape: enough to choose a conversation, without its body."""
        return {
            "conversation_id": self.conversation_id,
            "actor_id": self.actor_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": len(self.turns),
            "last_message": self.turns[-1].text[:160] if self.turns else "",
        }


def title_from(question: str) -> str:
    """A conversation is named after what opened it, trimmed at a word break so
    the list does not fill with half-words."""
    clean = " ".join(question.split())
    if len(clean) <= MAX_TITLE_CHARS:
        return clean or "New conversation"
    cut = clean[:MAX_TITLE_CHARS].rsplit(" ", 1)[0]
    return (cut or clean[:MAX_TITLE_CHARS]) + "…"


def load(store: ContextStore, conversation_id: str) -> Conversation | None:
    record = store.get(KIND_CONVERSATION, conversation_id)
    if record is None:
        return None
    return Conversation.model_validate(record.value)


def save(store: ContextStore, conversation: Conversation) -> Conversation:
    store.put(KIND_CONVERSATION, conversation.conversation_id, conversation.model_dump())
    return conversation


def append(
    store: ContextStore,
    conversation_id: str,
    *,
    actor_id: str,
    turns: list[Turn],
) -> Conversation:
    """Add turns, creating the conversation on first use.

    Oldest turns are dropped past ``MAX_TURNS`` so one long-running chat cannot
    grow a record without bound. Earlier versions still hold them, so nothing is
    actually lost, it just stops being carried forward.
    """
    existing = load(store, conversation_id)
    now = _now()
    if existing is None:
        first = next((t.text for t in turns if t.role == "you"), "")
        existing = Conversation(
            conversation_id=conversation_id,
            actor_id=actor_id,
            title=title_from(first),
            created_at=now,
            updated_at=now,
        )
    merged = [*existing.turns, *turns][-MAX_TURNS:]
    return save(
        store,
        existing.model_copy(update={"turns": merged, "updated_at": now}),
    )


def listing(store: ContextStore, actor_id: str = "", limit: int = MAX_LIST) -> list[dict[str, Any]]:
    """Conversations newest first. Without an actor, everything, which is what
    an operator looking at a shared dev workspace expects."""
    rows: list[Conversation] = []
    for record in store.query(KIND_CONVERSATION):
        try:
            conversation = Conversation.model_validate(record.value)
        except ValueError:
            continue  # a malformed row must not take the whole list down
        if actor_id and conversation.actor_id != actor_id:
            continue
        rows.append(conversation)
    rows.sort(key=lambda c: c.updated_at, reverse=True)
    return [c.summary() for c in rows[:limit]]
