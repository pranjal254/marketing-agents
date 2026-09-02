from __future__ import annotations

from shiftai_shared.control_plane import AuditLog, KillSwitch, RateBreaker, guard_layer4
from shiftai_shared.telemetry import InMemorySink, StsEmitter


def test_kill_switch_any_scope_pauses() -> None:
    ks = KillSwitch()
    assert not ks.check("agent-1", "client-1").paused
    ks.pause("client-1", "incident")
    state = ks.check("agent-1", "client-1")
    assert state.paused and state.reason == "incident"
    ks.resume("client-1")
    assert not ks.check("agent-1", "client-1").paused


def test_rate_breaker_trips_and_engages_kill_switch() -> None:
    ks = KillSwitch()
    rb = RateBreaker(window_minutes=10, max_auto_executions=3)
    for _ in range(3):
        rb.record_execution("agent-1")
    kill_state, breaker_state, reason = guard_layer4(ks, rb, "agent-1")
    assert breaker_state == "tripped"
    assert kill_state == "paused"
    assert reason == "rate_breaker_tripped"


def test_rate_breaker_window_expiry() -> None:
    rb = RateBreaker(window_minutes=1, max_auto_executions=2)
    rb.record_execution("a", now=0.0)
    rb.record_execution("a", now=1.0)
    assert rb.check("a", now=2.0) == "tripped"
    assert rb.check("a", now=120.0) == "ok"


def test_guard_clear_path() -> None:
    ks = KillSwitch()
    rb = RateBreaker(window_minutes=10, max_auto_executions=100)
    kill_state, breaker_state, reason = guard_layer4(ks, rb, "agent-1", "proc-1", "client-1")
    assert (kill_state, breaker_state, reason) == ("clear", "ok", None)


def test_audit_log_write_only_surface() -> None:
    exposed = {n for n in dir(AuditLog) if not n.startswith("_")}
    assert exposed == {"write"}


def test_audit_log_writes_layer() -> None:
    sink = InMemorySink()
    emitter = StsEmitter(
        sink,
        tenant_id="t",
        agent_id="a",
        agent_type="decision",
        config_version="1",
        environment="dev",
        risk_tier="low",
        data_classification="internal",
    )
    audit = AuditLog(emitter)
    audit.write(
        event_type="policy_check",
        case_id="c",
        trace_id="t",
        layer="L2",
        **{"shiftai.policy.decision": "allow"},
    )
    assert sink.records[0]["shiftai.layer"] == "L2"
