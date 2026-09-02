"""FastAPI dev bridge exposing the Campaign Identification agent to the Marketing
Studio UI. The human gates stay human: this service only carries the requester's
answers and the BU Campaign Lead's explicit decision into the agent.

Run:  uvicorn c2c_bridge.app:app --port 8787
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from campaign_identification import MODEL_ID
from campaign_identification.approval import ApprovalGateError
from campaign_identification.orchestration import AgentDeps, CampaignIdentificationAgent
from campaign_identification.persistence import (
    KIND_APPROVAL_TASK,
    KIND_CASE,
    KIND_GAP_REQUEST,
    LocalWorkspace,
)
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from shiftai_shared.business_capability import load_decision_config
from shiftai_shared.config import SharedSettings, load_settings
from shiftai_shared.context_store import SqliteContextStore
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.resilience import SqliteIdempotencyStore
from shiftai_shared.telemetry import JsonlSink

from c2c_bridge.bus import TeeSink, TelemetryBus

AGENTS_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = AGENTS_ROOT / "campaign-identification" / "config" / "campaign_identification.json"
DEFAULT_WORKDIR = AGENTS_ROOT / "bridge" / ".bridge-run"
DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class Bridge:
    """Holds the agent + its stores for the process lifetime."""

    def __init__(self, workdir: Path, settings: SharedSettings) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.settings = settings
        self.bus = TelemetryBus()
        self.store = SqliteContextStore(str(workdir / "context-store.sqlite"))
        self.workspace_dir = workdir / "workspace"
        config = load_decision_config(CONFIG_PATH)
        self.agent = CampaignIdentificationAgent(
            AgentDeps(
                provider=build_provider(settings),
                store=self.store,
                workspace=LocalWorkspace(str(self.workspace_dir)),
                sink=TeeSink(JsonlSink(str(workdir / "telemetry.jsonl")), self.bus),
                kill_switch=KillSwitch(),
                rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
                idempotency=SqliteIdempotencyStore(str(workdir / "idempotency.sqlite")),
                config=config,
                settings=settings,
            )
        )
        # One case at a time keeps SQLite happy and mirrors event-driven invocation.
        self.run_lock = threading.Lock()


class RequestIn(BaseModel):
    source: Literal["form", "plan", "calendar", "adhoc"] = "form"
    request: dict[str, Any]
    hold_for_verification: bool = False


class AnswersIn(BaseModel):
    answers: dict[str, Any]
    actor_id: str = Field(min_length=1)
    actor_role: str = "requester"
    release_after: bool = False


class ReviseIn(BaseModel):
    directive: str = ""
    aspects: list[str] = Field(default_factory=list)
    actor_id: str = Field(min_length=1)
    actor_role: str = "marketing-lead"


class ReleaseIn(BaseModel):
    actor_id: str = Field(min_length=1)
    actor_role: str = "marketing-lead"


class DecisionIn(BaseModel):
    decision: Literal["approved", "rejected", "returned"]
    actor_id: str = Field(min_length=1)
    actor_role: str = "bu-campaign-lead"
    notes: str | None = None


class KillSwitchIn(BaseModel):
    paused: bool
    reason: str = "paused from studio UI"


def _case_summary(key: str, value: dict[str, Any]) -> dict[str, Any]:
    brief = value.get("brief") or {}
    fields = {f["name"]: f["value"] for f in brief.get("fields", [])}
    return {
        "case_id": key,
        "status": value.get("status"),
        "action_class": value.get("action_class"),
        "campaign_id": value.get("campaign_id"),
        "topic": fields.get("offer_topic") or (value.get("request") or {}).get("offer_topic"),
        "business_unit": fields.get("business_unit")
        or (value.get("request") or {}).get("business_unit"),
        "vertical": fields.get("vertical") or (value.get("request") or {}).get("vertical"),
        "gap_rounds": value.get("gap_rounds", 0),
        "escalation_reason_code": value.get("escalation_reason_code"),
        "doc_ref": value.get("doc_ref"),
        "trace_id": value.get("trace_id"),
        "updated_at": value.get("updated_at"),
        "awaiting_since": value.get("awaiting_since"),
        "brief_version": value.get("brief_version"),
        "returned_note": value.get("returned_note"),
        "last_directive": value.get("last_directive"),
        "derived_fields": (value.get("request") or {}).get("derived_fields", {}),
        "request": value.get("request"),
    }


def _new_session_dir(root: Path) -> Path:
    from datetime import UTC, datetime
    from uuid import uuid4

    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return root / f"session-{stamp}-{uuid4().hex[:4]}"


def create_app(workdir: Path | None = None, settings: SharedSettings | None = None) -> FastAPI:
    root = workdir or Path(os.environ.get("BRIDGE_WORKDIR", str(DEFAULT_WORKDIR)))
    app_settings = settings or load_settings()
    holder: dict[str, Bridge] = {"bridge": Bridge(_new_session_dir(root), app_settings)}

    def bridge() -> Bridge:
        return holder["bridge"]

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(title="C2C Agent Bridge", version="0.1.0", lifespan=lifespan)
    app.state.bridge_holder = holder
    origins = os.environ.get("BRIDGE_CORS_ORIGINS", DEFAULT_ORIGINS).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "agent_id": bridge().agent.deps.config.agent_id,
            "config_version": bridge().agent.deps.config.version,
            "provider": bridge().settings.llm_provider,
            "model": MODEL_ID,
            "environment": bridge().settings.shiftai_environment,
            "kill_switch": (
                "paused"
                if bridge().agent.deps.kill_switch.check(bridge().agent.deps.config.agent_id).paused
                else "clear"
            ),
        }

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        config = bridge().agent.deps.config
        return {
            "agent_id": config.agent_id,
            "agent_name": "Campaign Identification Agent",
            "config_version": config.version,
            "action_classes": [a.model_dump() for a in config.action_class_taxonomy],
            "intake_schema": [f.model_dump() for f in config.intake_schema],
            "reason_codes": config.reason_codes,
            "provider": bridge().settings.llm_provider,
            "model": MODEL_ID,
        }

    # ------------------------------------------------------------------ actions
    @app.post("/api/requests")
    def submit_request(body: RequestIn) -> dict[str, Any]:
        with bridge().run_lock:
            outcome = bridge().agent.process_request(
                body.request, body.source, hold_for_verification=body.hold_for_verification
            )
        return outcome.model_dump()

    @app.post("/api/cases/{case_id}/answers")
    def submit_answers(case_id: str, body: AnswersIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = bridge().agent.submit_gap_answers(
                    case_id,
                    body.answers,
                    actor_role=body.actor_role,
                    actor_id=body.actor_id,
                    release_after=body.release_after,
                )
        except ApprovalGateError as exc:
            raise _gate_http_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/cases/{case_id}/decision")
    def record_decision(case_id: str, body: DecisionIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = bridge().agent.record_human_decision(
                    case_id,
                    body.decision,
                    actor_role=body.actor_role,
                    actor_id=body.actor_id,
                    notes=body.notes,
                )
        except ApprovalGateError as exc:
            raise _gate_http_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/cases/{case_id}/revise")
    def revise(case_id: str, body: ReviseIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = bridge().agent.revise_brief(
                    case_id,
                    directive=body.directive,
                    aspects=body.aspects,
                    actor_id=body.actor_id,
                    actor_role=body.actor_role,
                )
        except ApprovalGateError as exc:
            raise _gate_http_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/cases/{case_id}/release")
    def release(case_id: str, body: ReleaseIn) -> dict[str, Any]:
        try:
            with bridge().run_lock:
                outcome = bridge().agent.release_brief(
                    case_id, actor_id=body.actor_id, actor_role=body.actor_role
                )
        except ApprovalGateError as exc:
            raise _gate_http_error(exc) from exc
        return outcome.model_dump()

    @app.post("/api/control/kill-switch")
    def set_kill_switch(body: KillSwitchIn) -> dict[str, str]:
        agent_id = bridge().agent.deps.config.agent_id
        if body.paused:
            bridge().agent.deps.kill_switch.pause(agent_id, body.reason)
        else:
            bridge().agent.deps.kill_switch.resume(agent_id)
        return {"kill_switch": "paused" if body.paused else "clear"}

    @app.post("/api/control/reset")
    def reset_session() -> dict[str, str]:
        """Start a fresh dev session in a new directory. Nothing is deleted — prior
        session data stays on disk (append-only discipline holds even in dev)."""
        holder["bridge"] = Bridge(_new_session_dir(root), app_settings)
        return {"status": "reset", "workdir": str(holder["bridge"].workdir)}

    # ------------------------------------------------------------------ reads
    @app.get("/api/cases")
    def list_cases() -> list[dict[str, Any]]:
        records = bridge().store.query(KIND_CASE)
        summaries = [_case_summary(r.key, r.value) for r in records]
        summaries.sort(key=lambda s: str(s.get("updated_at", "")), reverse=True)
        return summaries

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str) -> dict[str, Any]:
        record = bridge().store.get(KIND_CASE, case_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown case {case_id}")
        gap = bridge().store.get(KIND_GAP_REQUEST, case_id)
        task = bridge().store.get(KIND_APPROVAL_TASK, case_id)
        return {
            "summary": _case_summary(case_id, record.value),
            "case": record.value,
            "gap_request": gap.value if gap else None,
            "approval_task": task.value if task else None,
        }

    @app.get("/api/telemetry")
    def telemetry(after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return bridge().bus.history(after_seq=after, limit=min(limit, 1000))

    @app.get("/api/documents/{name}")
    def document(name: str) -> FileResponse:
        safe = Path(name).name
        path = bridge().workspace_dir / safe
        if not path.is_file():
            raise HTTPException(status_code=404, detail="document not found")
        return FileResponse(
            path,
            filename=safe,
            media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        )

    # ------------------------------------------------------------------ live feed
    @app.get("/api/stream")
    async def stream(after: int = 0) -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            queue = bridge().bus.subscribe()
            try:
                for record in bridge().bus.history(after_seq=after):
                    yield f"data: {json.dumps(record)}\n\n"
                while True:
                    try:
                        record = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"data: {json.dumps(record)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bridge().bus.unsubscribe(queue)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _gate_http_error(exc: ApprovalGateError) -> HTTPException:
    status = 404 if str(exc).startswith("unknown case") else 409
    return HTTPException(status_code=status, detail=str(exc))


app = create_app()
