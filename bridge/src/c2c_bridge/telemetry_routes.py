"""Aggregated telemetry export for the ShiftAI Execution Studio.

Three routes, one job: let another application read a dashboard-ready rollup of
this process without replaying the raw STS stream.

  GET  /api/telemetry/export           the banked snapshot, served from the store
  POST /api/telemetry/export/refresh   recompute and bank a new one
  GET  /api/telemetry/export/history   when previous snapshots were taken

Why a banked snapshot rather than aggregating on every read. The raw stream is a
local append-only JSONL file in a per-session working directory, so it does not
survive a restart, and on a free hosting tier restarts are routine. Snapshots go
to the context store, which is Postgres whenever DATABASE_URL is set, so they do
persist. Each refresh aggregates whatever raw records sit after the last
snapshot cursor and merges that delta onto the banked figures, which means
totals only ever move forwards even though the underlying file keeps being
emptied underneath them.

Refreshes are rate limited to one per REFRESH_INTERVAL_HOURS (six by default).
Calling sooner is not an error: the route returns the banked snapshot with
``refreshed: false`` and a ``next_refresh_at``, so a polling client needs no
special case. ``?force=true`` overrides it for operators and tests.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from shiftai_shared.process import PROCESS_PACK
from shiftai_shared.telemetry.aggregate import (
    aggregate_records,
    empty_snapshot,
    merge_snapshots,
    record_is_after,
)

KIND_TELEMETRY_SNAPSHOT = "telemetry_snapshot"
TELEMETRY_FILENAME = "telemetry.jsonl"
DEFAULT_REFRESH_INTERVAL_HOURS = 6.0


def refresh_interval_hours() -> float:
    """Deployment decision, not agent code. Invalid values fall back rather than
    breaking the endpoint, because a bad env var must not take telemetry down."""
    raw = os.environ.get("TELEMETRY_EXPORT_INTERVAL_HOURS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_INTERVAL_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_REFRESH_INTERVAL_HOURS
    return value if value > 0 else DEFAULT_REFRESH_INTERVAL_HOURS


def _stage_labels() -> dict[str, str]:
    return {stage.stage_id: stage.label for stage in PROCESS_PACK.stages}


def _process_descriptor() -> dict[str, Any]:
    """The vocabulary a consuming dashboard needs to render steps it has never
    seen, shipped alongside the figures so the two cannot drift apart."""
    return {
        "name": PROCESS_PACK.process_name,
        "version": PROCESS_PACK.version,
        "label": PROCESS_PACK.label,
        "description": PROCESS_PACK.description,
        "stages": [
            {
                "stage_id": stage.stage_id,
                "ordinal": stage.ordinal,
                "label": stage.label,
                "description": stage.description,
            }
            for stage in PROCESS_PACK.stages
        ],
    }


def read_stream(path: Path) -> list[dict[str, Any]]:
    """Every record in the durable stream, each tagged with its line position.

    The position makes the snapshot cursor exact: two records sharing a
    timestamp still order deterministically. A malformed line is skipped rather
    than failing the export, since one bad line must not hide the other
    thousands.
    """
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                record["bridge.seq"] = index
                records.append(record)
    return records


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def register_telemetry_routes(app: FastAPI, bridge: Any) -> None:
    """``bridge`` is the zero-arg accessor returning the live Bridge instance."""

    def snapshot_key() -> str:
        return PROCESS_PACK.process_name

    def load_snapshot() -> dict[str, Any] | None:
        record = bridge().store.get(KIND_TELEMETRY_SNAPSHOT, snapshot_key())
        return dict(record.value) if record else None

    def envelope(snapshot: dict[str, Any], **extra: Any) -> dict[str, Any]:
        generated = _parse_iso(snapshot.get("generated_at"))
        interval = refresh_interval_hours()
        next_refresh = generated + timedelta(hours=interval) if generated else None
        return {
            "process": _process_descriptor(),
            "refresh_interval_hours": interval,
            "next_refresh_at": _iso(next_refresh) if next_refresh else None,
            "snapshot": snapshot,
            **extra,
        }

    @app.get("/api/telemetry/export")
    def export() -> dict[str, Any]:
        """The banked rollup. Never recomputes, so it stays cheap to poll."""
        snapshot = load_snapshot()
        if snapshot is None:
            # Nothing banked yet. An empty snapshot is the honest answer and has
            # the same shape, so a client needs no separate first-run path.
            return envelope(empty_snapshot(), banked=False)
        return envelope(snapshot, banked=True)

    @app.post("/api/telemetry/export/refresh")
    def refresh(force: bool = False) -> dict[str, Any]:
        """Aggregate everything after the last cursor and bank the result."""
        previous = load_snapshot()
        interval = refresh_interval_hours()
        generated = _parse_iso((previous or {}).get("generated_at"))
        now = datetime.now(tz=UTC)

        if previous is not None and generated is not None and not force:
            due_at = generated + timedelta(hours=interval)
            if now < due_at:
                return envelope(
                    previous,
                    banked=True,
                    refreshed=False,
                    reason=(
                        f"last refreshed at {_iso(generated)}; refreshes are limited to "
                        f"one per {interval:g} hours"
                    ),
                    records_added=0,
                )

        base = previous if previous is not None else empty_snapshot()
        cursor = base.get("cursor")
        stream = read_stream(bridge().workdir / TELEMETRY_FILENAME)
        fresh = [record for record in stream if record_is_after(record, cursor)]
        delta = aggregate_records(fresh, stage_labels=_stage_labels())
        merged = merge_snapshots(base, delta)
        bridge().store.put(KIND_TELEMETRY_SNAPSHOT, snapshot_key(), merged)
        return envelope(
            merged,
            banked=True,
            refreshed=True,
            records_added=len(fresh),
            records_in_stream=len(stream),
        )

    @app.get("/api/telemetry/export/history")
    def history() -> dict[str, Any]:
        """Metadata for every banked snapshot, newest first. Figures are left
        out: this answers when and how much, not what."""
        versions = bridge().store.get_all_versions(KIND_TELEMETRY_SNAPSHOT, snapshot_key())
        return {
            "process": _process_descriptor()["name"],
            "refresh_interval_hours": refresh_interval_hours(),
            "snapshots": [
                {
                    "version": record.version,
                    "stored_at": record.created_at,
                    "generated_at": record.value.get("generated_at"),
                    "refresh_count": record.value.get("refresh_count"),
                    "records": record.value.get("totals", {}).get("records"),
                    "cases": record.value.get("totals", {}).get("cases"),
                    "cost_usd": record.value.get("totals", {}).get("cost_usd"),
                    "window": record.value.get("window"),
                }
                for record in sorted(versions, key=lambda r: r.version, reverse=True)
            ],
        }
