"""Export routes over HTTP: the rate limit, the merge across a cleared stream,
and the stage breakdown a dashboard reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shiftai_shared.config import SharedSettings

from c2c_bridge.app import create_app
from c2c_bridge.telemetry_routes import TELEMETRY_FILENAME


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # The export reads the raw stream off disk, so no agent run and no provider
    # is needed; the tests write records directly. The interval var is cleared
    # so a developer machine cannot change what the rate-limit tests assert.
    monkeypatch.delenv("TELEMETRY_EXPORT_INTERVAL_HOURS", raising=False)
    app = create_app(
        workdir=tmp_path / "run",
        settings=SharedSettings(_env_file=None, LLM_PROVIDER="mock"),
    )
    return TestClient(app)


def _bridge(client: TestClient) -> Any:
    return client.app.state.bridge_holder["bridge"]


def _stream_path(client: TestClient) -> Any:
    return _bridge(client).workdir / TELEMETRY_FILENAME


def _append(client: TestClient, records: list[dict[str, Any]]) -> None:
    with open(_stream_path(client), "a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _record(seq: int, *, stage: str = "drafting", ordinal: int = 4, cost: float = 0.01) -> dict:
    return {
        "shiftai.schema.version": "2.0.0",
        "shiftai.tenant.id": "levelshift",
        "shiftai.agent.id": "content_repurposing",
        "shiftai.agent.type": "orchestrator",
        "shiftai.process.name": "content-to-campaign",
        "shiftai.process.version": "1.0.0",
        "shiftai.stage.id": stage,
        "shiftai.stage.ordinal": ordinal,
        "deployment.environment.name": "dev",
        "shiftai.event.type": "decision_made",
        "shiftai.case.id": f"case_{seq}",
        "shiftai.trace.id": f"trace_{seq}",
        "shiftai.timestamp": f"2026-10-04T12:{seq:02d}:00Z",
        "shiftai.cost.amount": cost,
        "shiftai.span.duration_ms": 1000 + seq,
    }


def test_export_before_any_refresh_is_an_empty_snapshot_not_an_error(
    client: TestClient,
) -> None:
    body = client.get("/api/telemetry/export").json()
    assert body["banked"] is False
    assert body["snapshot"]["totals"]["records"] == 0
    # The vocabulary ships even when there are no figures yet.
    assert [s["ordinal"] for s in body["process"]["stages"]] == list(range(1, 10))
    assert body["refresh_interval_hours"] == 6.0


def test_refresh_banks_a_snapshot_the_export_then_serves(client: TestClient) -> None:
    _append(client, [_record(1), _record(2, stage="compliance", ordinal=7)])

    refreshed = client.post("/api/telemetry/export/refresh").json()
    assert refreshed["refreshed"] is True
    assert refreshed["records_added"] == 2
    assert refreshed["snapshot"]["totals"]["records"] == 2
    assert refreshed["snapshot"]["totals"]["cost_usd"] == pytest.approx(0.02)

    stages = {row["stage_id"]: row for row in refreshed["snapshot"]["by_stage"]}
    assert stages["compliance"]["stage_label"] == "Compliance"
    assert [row["stage_id"] for row in refreshed["snapshot"]["by_stage"]] == [
        "drafting",
        "compliance",
    ]

    served = client.get("/api/telemetry/export").json()
    assert served["banked"] is True
    assert served["snapshot"]["totals"]["records"] == 2
    assert served["next_refresh_at"] is not None


def test_second_refresh_inside_the_window_is_declined_but_still_returns_data(
    client: TestClient,
) -> None:
    _append(client, [_record(1)])
    client.post("/api/telemetry/export/refresh")

    _append(client, [_record(2)])
    again = client.post("/api/telemetry/export/refresh").json()
    assert again["refreshed"] is False
    assert again["records_added"] == 0
    assert "six" in again["reason"] or "6 hours" in again["reason"]
    # The caller still gets usable figures, just the previous ones.
    assert again["snapshot"]["totals"]["records"] == 1


def test_forced_refresh_adds_only_records_after_the_cursor(client: TestClient) -> None:
    _append(client, [_record(1), _record(2)])
    first = client.post("/api/telemetry/export/refresh").json()
    assert first["snapshot"]["totals"]["records"] == 2

    _append(client, [_record(3)])
    second = client.post("/api/telemetry/export/refresh?force=true").json()
    assert second["refreshed"] is True
    # Three records in the file, but only the new one was counted again.
    assert second["records_in_stream"] == 3
    assert second["records_added"] == 1
    assert second["snapshot"]["totals"]["records"] == 3
    assert second["snapshot"]["refresh_count"] == 2


def test_totals_survive_the_raw_stream_being_cleared(client: TestClient) -> None:
    """A restart empties the JSONL file. Banked figures must not go backwards."""
    _append(client, [_record(1), _record(2)])
    client.post("/api/telemetry/export/refresh")

    _stream_path(client).write_text("", encoding="utf-8")
    _append(client, [_record(5, stage="signoff", ordinal=9)])

    after = client.post("/api/telemetry/export/refresh?force=true").json()
    assert after["snapshot"]["totals"]["records"] == 3
    assert {row["stage_id"] for row in after["snapshot"]["by_stage"]} == {
        "drafting",
        "signoff",
    }


def test_malformed_line_is_skipped_not_fatal(client: TestClient) -> None:
    _append(client, [_record(1)])
    with open(_stream_path(client), "a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
    _append(client, [_record(2)])

    body = client.post("/api/telemetry/export/refresh").json()
    assert body["refreshed"] is True
    assert body["snapshot"]["totals"]["records"] == 2


def test_history_lists_each_banked_snapshot_newest_first(client: TestClient) -> None:
    _append(client, [_record(1)])
    client.post("/api/telemetry/export/refresh")
    _append(client, [_record(2)])
    client.post("/api/telemetry/export/refresh?force=true")

    history = client.get("/api/telemetry/export/history").json()
    versions = [row["version"] for row in history["snapshots"]]
    assert versions == sorted(versions, reverse=True)
    assert len(versions) >= 2
    assert history["snapshots"][0]["records"] == 2
