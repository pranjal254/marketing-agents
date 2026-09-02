"""V2 Cross-Agent Standard B envelope: trace/run/span identity, latency breakdown, cost.

Latency categories (llm / api / queue) and run ids are additive attributes on top of
the STS v2 core — allowed passthrough, never replacing schema-required fields.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from shiftai_shared.config import CACHE_READ_INPUT_RATE, DEFAULT_RATE_CARD


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _lookup_rates(
    card: dict[str, tuple[float, float]], model: str
) -> tuple[float, float] | None:
    """Exact key first; otherwise the longest card key the model id starts with
    (providers append version/date suffixes to the base model name)."""
    exact = card.get(model)
    if exact is not None:
        return exact
    best: tuple[float, float] | None = None
    best_len = 0
    for key, rates in card.items():
        if model.startswith(key) and len(key) > best_len:
            best, best_len = rates, len(key)
    return best


def rate_card_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    rate_card: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """USD cost from the rate card; None when the model has no card (emit absent, never guess)."""
    card = rate_card if rate_card is not None else DEFAULT_RATE_CARD
    rates = _lookup_rates(card, model)
    if rates is None:
        return None
    in_rate, out_rate = rates
    uncached_in = max(input_tokens - cache_read_input_tokens, 0)
    cost = (
        uncached_in * in_rate
        + cache_read_input_tokens * in_rate * CACHE_READ_INPUT_RATE
        + output_tokens * out_rate
    ) / 1_000_000
    return round(cost, 6)


def response_cost(
    response_model: str,
    request_model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    rate_card: dict[str, tuple[float, float]] | None = None,
) -> float | None:
    """Cost priced against the model that ACTUALLY answered (multi-provider fleets:
    a dev run on an Azure model must not be billed at Claude rates). Falls back to
    the requested/target model's rates only when the responding model has no card
    entry — the dev substitute is then a modeled production cost."""
    actual = rate_card_cost(
        response_model, input_tokens, output_tokens, cache_read_input_tokens, rate_card
    )
    if actual is not None:
        return actual
    return rate_card_cost(
        request_model, input_tokens, output_tokens, cache_read_input_tokens, rate_card
    )


@dataclass
class SpanRecord:
    span_id: str
    name: str
    category: str  # "llm" | "api" | "queue" | "other"
    duration_ms: int


@dataclass
class RunContext:
    """Identity + accumulators for one processing run of one case."""

    case_id: str
    trace_id: str
    run_id: str = field(default_factory=lambda: new_id("run"))
    monotonic: Callable[[], float] = time.monotonic
    spans: list[SpanRecord] = field(default_factory=list)
    total_cost_usd: float = 0.0
    _started: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self._started = self.monotonic()

    @contextmanager
    def span(self, name: str, category: str = "other") -> Iterator[SpanRecord]:
        rec = SpanRecord(span_id=new_id("span"), name=name, category=category, duration_ms=0)
        start = self.monotonic()
        try:
            yield rec
        finally:
            rec.duration_ms = max(int((self.monotonic() - start) * 1000), 0)
            self.spans.append(rec)

    def add_cost(self, amount_usd: float | None) -> None:
        if amount_usd is not None:
            self.total_cost_usd = round(self.total_cost_usd + amount_usd, 6)

    def latency_breakdown_ms(self) -> dict[str, int]:
        by: dict[str, int] = {"llm": 0, "api": 0, "queue": 0}
        for s in self.spans:
            if s.category in by:
                by[s.category] += s.duration_ms
        by["total"] = max(int((self.monotonic() - self._started) * 1000), 0)
        return by

    def run_attributes(self) -> dict[str, Any]:
        """Additive Standard-B attributes carried on every record of this run."""
        return {"shiftai.run.id": self.run_id}

    def summary_attributes(self) -> dict[str, Any]:
        lat = self.latency_breakdown_ms()
        attrs: dict[str, Any] = {
            "shiftai.run.id": self.run_id,
            "shiftai.span.duration_ms": lat["total"],
            "shiftai.latency.llm_ms": lat["llm"],
            "shiftai.latency.api_ms": lat["api"],
            "shiftai.latency.queue_ms": lat["queue"],
        }
        if self.total_cost_usd > 0:
            attrs.update(
                {
                    "shiftai.cost.amount": self.total_cost_usd,
                    "shiftai.cost.currency": "USD",
                    "shiftai.cost.model": "rate_card",
                    "shiftai.cost.scope": "run_total",
                }
            )
        return attrs
