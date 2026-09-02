"""In-process telemetry bus: tees every STS record to subscribers (SSE clients)
while the primary JSONL sink keeps the durable append-only stream."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

from shiftai_shared.telemetry import TelemetrySink

HISTORY_LIMIT = 2000


class TelemetryBus:
    def __init__(self) -> None:
        self._history: list[dict[str, Any]] = []
        self._subscribers: list[
            tuple[asyncio.Queue[dict[str, Any]], asyncio.AbstractEventLoop]
        ] = []
        self._lock = threading.Lock()
        self._seq = 0

    def publish(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            stamped = {**record, "bridge.seq": self._seq}
            self._history.append(stamped)
            if len(self._history) > HISTORY_LIMIT:
                self._history = self._history[-HISTORY_LIMIT:]
            targets = list(self._subscribers)
        for queue, loop in targets:
            # a closed loop just means the subscriber is gone; unsubscribe cleans up
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(queue.put_nowait, stamped)

    def history(self, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = [r for r in self._history if int(r.get("bridge.seq", 0)) > after_seq]
        return rows[-limit:]

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.append((queue, loop))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = [(q, lp) for q, lp in self._subscribers if q is not queue]


class TeeSink(TelemetrySink):
    """Append-only tee: durable JSONL first, then the live bus."""

    def __init__(self, primary: TelemetrySink, bus: TelemetryBus) -> None:
        self._primary = primary
        self._bus = bus

    def emit(self, record: dict[str, Any]) -> None:
        self._primary.emit(record)
        self._bus.publish(record)
