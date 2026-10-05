"""Provider-agnostic LLM interface (reference architecture: the reasoning interface
must never assume a specific provider anywhere in its code path).

Production: Anthropic (Claude). Dev/test: Azure OpenAI or the in-process mock.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field


class SystemBlock(BaseModel):
    """One system-prompt block. ``cache=True`` marks it as a stable, cacheable block
    (prompt caching is mandatory on all Claude calls — Cross-Agent Standard A)."""

    text: str
    cache: bool = True


class LLMResponse(BaseModel):
    text: str
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    finish_reason: str | None = None


def effective_model(provider: Any, requested: str) -> str:
    """The model that will actually answer a call for ``requested``.

    A provider may substitute: the Azure binding ignores the Claude id the spec
    routes to and serves the request from its configured deployment. Telemetry
    already records both, but anything shown to a person should name the model
    that really ran, or the screen quietly misreports what the deployment is
    doing.

    Duck-typed rather than part of the Protocol, so a provider that never
    substitutes needs no code and test doubles keep working untouched.
    """
    resolve = getattr(provider, "effective_model_name", None)
    return str(resolve(requested)) if callable(resolve) else requested


class LLMProvider(Protocol):
    def complete(
        self,
        *,
        system: Sequence[SystemBlock],
        user: str,
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
    ) -> LLMResponse: ...


class MockLLMProvider:
    """Deterministic provider for unit tests and offline dev runs.

    ``script`` maps a matcher over the user message to a canned response text;
    ``default`` is returned when nothing matches. Records every call for assertions.
    """

    def __init__(
        self,
        default: str = "{}",
        script: Sequence[tuple[Callable[[str], bool], str]] = (),
        model_name: str = "mock-model",
    ) -> None:
        self.default = default
        self.script = list(script)
        self.model_name = model_name
        self.calls: list[dict[str, object]] = []

    def effective_model_name(self, requested: str) -> str:
        return self.model_name

    def complete(
        self,
        *,
        system: Sequence[SystemBlock],
        user: str,
        model: str,
        max_tokens: int,
        temperature: float = 0.0,
        timeout_s: float = 60.0,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": [b.model_dump() for b in system],
                "user": user,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        text = self.default
        for matches, response in self.script:
            if matches(user):
                text = response
                break
        return LLMResponse(
            text=text,
            model=self.model_name,
            input_tokens=len(user) // 4,
            output_tokens=len(text) // 4,
            cache_read_input_tokens=0,
            finish_reason="end_turn",
        )
