"""Claude via the Anthropic SDK, with prompt caching on stable system blocks.

Cross-Agent Standard A: prompt caching on system prompts + stable context is
mandatory. Timeouts and 3-retry exponential backoff apply to every call.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import anthropic

from shiftai_shared.llm.provider import LLMResponse, SystemBlock
from shiftai_shared.resilience import PermanentError, TransientError, with_retries


class AnthropicClient:
    def __init__(self, api_key: str | None = None, retries: int = 3) -> None:
        # The SDK reads ANTHROPIC_API_KEY from the environment when api_key is None.
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._retries = retries

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
        system_param: list[dict[str, Any]] = []
        for block in system:
            entry: dict[str, Any] = {"type": "text", "text": block.text}
            if block.cache:
                entry["cache_control"] = {"type": "ephemeral"}
            system_param.append(entry)

        # `temperature` is part of the provider-agnostic interface (Azure uses it) but
        # is not sent to Claude 5: the Messages API drops it in favor of adaptive
        # thinking, which the spec's "adaptive thinking, effort medium" relies on.
        del temperature

        def call() -> Any:
            try:
                return self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_param,  # type: ignore[arg-type]
                    messages=[{"role": "user", "content": user}],
                    timeout=timeout_s,
                )
            except (
                anthropic.APITimeoutError,
                anthropic.APIConnectionError,
                anthropic.RateLimitError,
                anthropic.InternalServerError,
            ) as exc:
                raise TransientError(str(exc)) from exc
            except anthropic.APIStatusError as exc:
                raise PermanentError(str(exc)) from exc

        response = with_retries(call, retries=self._retries)
        text = "".join(
            part.text for part in response.content if getattr(part, "type", "") == "text"
        )
        usage = response.usage
        return LLMResponse(
            text=text,
            model=response.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            finish_reason=response.stop_reason,
        )
