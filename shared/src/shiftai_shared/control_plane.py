"""Control Plane primitives: kill switch, rate breaker, append-only audit.

Zero domain knowledge lives here (kit hard rule 4). The kill switch is checked
immediately before every Layer 4 action; a tripped rate breaker engages the kill
switch before anything else proceeds (kit hard rules 8).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from shiftai_shared.telemetry.emitter import StsEmitter, utc_now_iso


@dataclass(frozen=True)
class PauseState:
    paused: bool
    reason: str | None = None


class KillSwitch:
    """Scope-keyed pause flags (agent / process / client). Any active scope pauses."""

    def __init__(self) -> None:
        self._flags: dict[str, str] = {}
        self._lock = threading.Lock()

    def pause(self, scope_id: str, reason: str) -> None:
        with self._lock:
            self._flags[scope_id] = reason

    def resume(self, scope_id: str) -> None:
        with self._lock:
            self._flags.pop(scope_id, None)

    def check(self, *scope_ids: str) -> PauseState:
        with self._lock:
            for sid in scope_ids:
                if sid in self._flags:
                    return PauseState(paused=True, reason=self._flags[sid])
        return PauseState(paused=False)


class RateBreaker:
    """Trips on anomalous aggregate auto-execution volume inside a sliding window."""

    def __init__(self, window_minutes: int, max_auto_executions: int) -> None:
        self.window_minutes = window_minutes
        self.max_auto_executions = max_auto_executions
        self._executions: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def record_execution(self, agent_id: str, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        with self._lock:
            self._executions.setdefault(agent_id, []).append(ts)

    def check(self, agent_id: str, now: float | None = None) -> str:
        ts = time.time() if now is None else now
        cutoff = ts - self.window_minutes * 60
        with self._lock:
            recent = [t for t in self._executions.get(agent_id, []) if t >= cutoff]
            self._executions[agent_id] = recent
            return "tripped" if len(recent) >= self.max_auto_executions else "ok"


def guard_layer4(
    kill_switch: KillSwitch,
    rate_breaker: RateBreaker,
    agent_id: str,
    *extra_scope_ids: str,
) -> tuple[str, str, str | None]:
    """The mandatory pre-action check.

    Returns (kill_switch_state, rate_breaker_state, pause_reason).
    A tripped breaker engages the kill switch for the agent before returning.
    """
    breaker = rate_breaker.check(agent_id)
    if breaker == "tripped":
        kill_switch.pause(agent_id, "rate_breaker_tripped")
    pause = kill_switch.check(agent_id, *extra_scope_ids)
    return ("paused" if pause.paused else "clear", breaker, pause.reason)


class AuditLog:
    """Append-only audit over the STS stream (telemetry IS the audit trail).

    Exposes write() only — no update or delete operation exists anywhere (kit rule 7).
    """

    def __init__(self, emitter: StsEmitter) -> None:
        self._emitter = emitter

    def write(
        self,
        *,
        event_type: str,
        case_id: str,
        trace_id: str,
        layer: str | None = None,
        **attributes: Any,
    ) -> dict[str, Any]:
        if layer is not None:
            attributes["shiftai.layer"] = layer
        return self._emitter.emit(
            event_type,
            case_id=case_id,
            trace_id=trace_id,
            timestamp=utc_now_iso(),
            **attributes,
        )
