"""FastAPI dev bridge exposing the Campaign Identification and Campaign-in-a-Box
agents to the Marketing Studio UI. The human gates stay human: this service only
carries requester answers and explicit identity-stamped decisions into the agents.

Run:  uvicorn c2c_bridge.app:app --port 8787

Hosted deployments MUST set BRIDGE_API_TOKEN: every /api route except /api/health
then requires `Authorization: Bearer <token>` (or `?token=` for browser-navigated
requests: SSE stream and document downloads, which cannot carry headers).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from c2c_campaign_box import MODEL_ID as BOX_MODEL_ID
from c2c_collaboration import MODEL_ID as COLLAB_MODEL_ID
from c2c_content_repurposing import MODEL_ID as REPURPOSE_MODEL_ID
from campaign_identification import MODEL_ID
from campaign_identification.approval import ApprovalGateError
from campaign_identification.orchestration import AgentDeps, CampaignIdentificationAgent
from campaign_identification.persistence import (
    KIND_APPROVAL_TASK,
    KIND_CASE,
    KIND_GAP_REQUEST,
    LocalWorkspace,
)
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from shiftai_shared.business_capability import load_decision_config
from shiftai_shared.config import SharedSettings, load_settings
from shiftai_shared.context_store import (
    build_context_store,
    build_idempotency_store,
    store_backend,
)
from shiftai_shared.control_plane import KillSwitch, RateBreaker
from shiftai_shared.llm import build_provider
from shiftai_shared.telemetry import JsonlSink

from c2c_bridge.bus import TeeSink, TelemetryBus
from c2c_bridge.seed import seed_dev_workspace

AGENTS_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = AGENTS_ROOT / "campaign-identification" / "config" / "campaign_identification.json"
BOX_CONFIG_PATH = AGENTS_ROOT / "campaign-in-a-box" / "config" / "campaign_in_a_box.json"
REPURPOSE_CONFIG_PATH = AGENTS_ROOT / "content-repurposing" / "config" / "content_repurposing.json"
COLLAB_CONFIG_PATH = (
    AGENTS_ROOT / "collaboration-iteration" / "config" / "collaboration_iteration.json"
)
DEFAULT_WORKDIR = AGENTS_ROOT / "bridge" / ".bridge-run"
DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


class Bridge:
    """Holds both agents + their stores for the process lifetime. One session =
    one store, one telemetry bus, one kill switch (scope-keyed per agent) — so an
    approved brief from Agent 1 flows straight into Agent 2's planning pass and
    both agents' STS records ride one stream."""

    def __init__(self, workdir: Path, settings: SharedSettings) -> None:
        from c2c_campaign_box.agent_config import load_orchestrator_config
        from c2c_campaign_box.orchestration import (
            CampaignBoxOrchestrator,
            OrchestratorDeps,
        )
        from c2c_campaign_box.repository import LocalRepositoryIndex
        from c2c_campaign_box.workspace import LocalCampaignWorkspace
        from shiftai_shared.brand import load_brand_rules
        from shiftai_shared.semrush import SemrushClient

        workdir.mkdir(parents=True, exist_ok=True)
        self.workdir = workdir
        self.settings = settings
        self.bus = TelemetryBus()
        # Backend is an environment decision: DATABASE_URL set → tenant-scoped
        # Postgres (state + idempotency survive restarts); unset → per-session
        # SQLite exactly as before. Agents are unaware either way.
        self.store_backend = store_backend(settings)
        self.store = build_context_store(settings, str(workdir / "context-store.sqlite"))
        self.workspace_dir = workdir / "workspace"
        self.kill_switch = KillSwitch()
        sink = TeeSink(JsonlSink(str(workdir / "telemetry.jsonl")), self.bus)
        idempotency = build_idempotency_store(settings, str(workdir / "idempotency.sqlite"))
        config = load_decision_config(CONFIG_PATH)
        self.agent = CampaignIdentificationAgent(
            AgentDeps(
                provider=build_provider(settings),
                store=self.store,
                workspace=LocalWorkspace(str(self.workspace_dir)),
                sink=sink,
                kill_switch=self.kill_switch,
                rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
                idempotency=idempotency,
                config=config,
                settings=settings,
            )
        )
        # Agent 2 — dev bindings: local campaign workspace + seeded sample
        # repository / intel library; SemRush only when a key is configured
        # (otherwise intel-library-only fallback, flagged per spec).
        self.box_workspace_dir = workdir / "box-workspace"
        repository_dir = workdir / "repository"
        seed_dev_workspace(self.box_workspace_dir, repository_dir)
        box_config = load_orchestrator_config(BOX_CONFIG_PATH)
        intel_source = None
        if settings.semrush_api_key is not None:
            intel_source = SemrushClient(
                settings.semrush_api_key.get_secret_value(),
                database=settings.semrush_database,
            )
        self.box = CampaignBoxOrchestrator(
            OrchestratorDeps(
                provider=build_provider(settings),
                store=self.store,
                workspace=LocalCampaignWorkspace(str(self.box_workspace_dir)),
                repository=LocalRepositoryIndex(
                    str(repository_dir), box_config.fitness_weights
                ),
                intel_source=intel_source,
                sink=sink,
                kill_switch=self.kill_switch,
                rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
                idempotency=idempotency,
                config=box_config,
                settings=settings,
                brand_rules=load_brand_rules(),
            )
        )
        # Agent 3 — shares the store, telemetry stream, kill switch and the SAME
        # campaign workspace Agent 2 created (drafts land in {campaign}/drafts).
        from c2c_content_repurposing.agent_config import load_repurposing_config
        from c2c_content_repurposing.orchestration import (
            ContentRepurposingAgent,
            RepurposingDeps,
        )

        self.repurposer = ContentRepurposingAgent(
            RepurposingDeps(
                provider=build_provider(settings),
                store=self.store,
                workspace=LocalCampaignWorkspace(str(self.box_workspace_dir)),
                sink=sink,
                kill_switch=self.kill_switch,
                rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
                idempotency=idempotency,
                config=load_repurposing_config(REPURPOSE_CONFIG_PATH),
                settings=settings,
                brand_rules=load_brand_rules(),
            )
        )
        # Agent 4 — the review-cycle manager. Its outbound signals bind to the
        # co-hosted agents (BridgeSignals): flagship confirm → Agent 3 fan-out
        # unlock, derivative confirm → Agent 2 packaging registry, structural
        # feedback → Agent 3 rework. The old dev stand-ins are retired.
        from c2c_collaboration.agent_config import load_collaboration_config
        from c2c_collaboration.orchestration import CollaborationAgent, CollaborationDeps

        from c2c_bridge.signals import BridgeSignals

        self.collab = CollaborationAgent(
            CollaborationDeps(
                provider=build_provider(settings),
                store=self.store,
                workspace=LocalCampaignWorkspace(str(self.box_workspace_dir)),
                sink=sink,
                kill_switch=self.kill_switch,
                rate_breaker=RateBreaker(window_minutes=60, max_auto_executions=100),
                idempotency=idempotency,
                config=load_collaboration_config(COLLAB_CONFIG_PATH),
                settings=settings,
                brand_rules=load_brand_rules(),
                signals=BridgeSignals(lambda: self),
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


def create_app(
    workdir: Path | None = None,
    settings: SharedSettings | None = None,
    api_token: str | None = None,
) -> FastAPI:
    root = workdir or Path(os.environ.get("BRIDGE_WORKDIR", str(DEFAULT_WORKDIR)))
    app_settings = settings or load_settings()
    token = (api_token if api_token is not None else os.environ.get("BRIDGE_API_TOKEN", "")).strip()
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

    if token:
        # Hosted mode: shared-secret gate on every API route. /api/health stays
        # open (platform health checks); OPTIONS passes so CORS preflight works
        # (this middleware runs outside the CORS layer). Browser-navigated
        # requests (SSE stream, document downloads) supply ?token= because they
        # cannot carry an Authorization header.
        @app.middleware("http")
        async def require_token(request: Request, call_next: Any) -> Response:
            path = request.url.path
            if request.method == "OPTIONS" or path == "/api/health" or not path.startswith("/api"):
                return await call_next(request)  # type: ignore[no-any-return]
            header = request.headers.get("authorization", "")
            supplied = header[7:] if header.lower().startswith("bearer ") else (
                request.query_params.get("token", "")
            )
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return await call_next(request)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "agent_id": bridge().agent.deps.config.agent_id,
            "config_version": bridge().agent.deps.config.version,
            "provider": bridge().settings.llm_provider,
            "model": MODEL_ID,
            "store": bridge().store_backend,
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
            "box": {
                "agent_id": bridge().box.deps.config.agent_id,
                "agent_name": "Campaign-in-a-Box Orchestrator",
                "config_version": bridge().box.deps.config.version,
                "model": BOX_MODEL_ID,
                "composition": [
                    c.model_dump() for c in bridge().box.deps.config.composition
                ],
                "composition_status": bridge().box.deps.config.composition_status,
                "reason_codes": bridge().box.deps.config.reason_codes,
                "intel_mode": (
                    "semrush_plus_library"
                    if bridge().box.deps.intel_source is not None
                    else "intel_library_only"
                ),
            },
            "repurposing": {
                "agent_id": bridge().repurposer.deps.config.agent_id,
                "agent_name": "Content Repurposing Agent",
                "config_version": bridge().repurposer.deps.config.version,
                "model": REPURPOSE_MODEL_ID,
                "recipe_status": bridge().repurposer.deps.config.recipe_status,
                "recipes": [
                    r.model_dump() for r in bridge().repurposer.deps.config.recipes
                ],
                "reason_codes": bridge().repurposer.deps.config.reason_codes,
            },
            "collaboration": {
                "agent_id": bridge().collab.deps.config.agent_id,
                "agent_name": "Content Collaboration & Iteration Agent",
                "config_version": bridge().collab.deps.config.version,
                "model": COLLAB_MODEL_ID,
                "reviewer_map_status": bridge().collab.deps.config.reviewer_map_status,
                "reviewer_map": {
                    gate: [s.model_dump() for s in slots]
                    for gate, slots in bridge().collab.deps.config.reviewer_map.items()
                },
                "reason_codes": bridge().collab.deps.config.reason_codes,
            },
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
        # One governance switch pauses every agent in the session (scope-keyed).
        agent_ids = (
            bridge().agent.deps.config.agent_id,
            bridge().box.deps.config.agent_id,
            bridge().repurposer.deps.config.agent_id,
            bridge().collab.deps.config.agent_id,
        )
        for agent_id in agent_ids:
            if body.paused:
                bridge().kill_switch.pause(agent_id, body.reason)
            else:
                bridge().kill_switch.resume(agent_id)
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

    # ---------------------------------------------- Agent 2: Campaign-in-a-Box
    from c2c_bridge.box_routes import register_box_routes

    register_box_routes(app, bridge)

    # ---------------------------------------------- Agent 3: Content Repurposing
    from c2c_bridge.repurpose_routes import register_repurpose_routes

    register_repurpose_routes(app, bridge)

    # -------------------------------------- Agent 4: Collaboration & Iteration
    from c2c_bridge.review_routes import register_review_routes

    register_review_routes(app, bridge)

    return app


def _gate_http_error(exc: ApprovalGateError) -> HTTPException:
    status = 404 if str(exc).startswith("unknown case") else 409
    return HTTPException(status_code=status, detail=str(exc))


app = create_app()
