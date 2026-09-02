"""Provider-agnostic LLM interface (reference architecture: the reasoning interface
must never assume a specific provider anywhere in its code path).

Production: Anthropic (Claude). Dev/test: Azure OpenAI or the in-process mock.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

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
