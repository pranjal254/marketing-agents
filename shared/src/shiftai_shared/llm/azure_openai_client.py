"""Azure OpenAI provider — dev/test substitute only (dev environment has Azure GPT,
not Claude). Same interface; the caller's Claude model id is recorded but the Azure
deployment serves the request. Production stays on Anthropic per the spec.

Deployment differences are absorbed here: reasoning-class models (gpt-5.x, o-series)
require ``max_completion_tokens`` and reject explicit ``temperature``; older chat
models take ``max_tokens``/``temperature``. The client adapts on the API's
unsupported-parameter 400s, so callers stay provider-agnostic.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import openai

from shiftai_shared.llm.provider import LLMResponse, SystemBlock
from shiftai_shared.resilience import PermanentError, TransientError, with_retries


class AzureOpenAIClient:
    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        deployment: str,
        api_version: str = "2024-06-01",
        retries: int = 3,
    ) -> None:
        self._client = openai.AzureOpenAI(
            azure_endpoint=endpoint, api_key=api_key, api_version=api_version
        )
        self._deployment = deployment
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
        # Azure has no cache_control; blocks are concatenated. `model` (the Claude id
        # the spec routes to) is ignored in favor of the configured deployment.
        system_text = "\n\n".join(block.text for block in system)
        params: dict[str, Any] = {
            "model": self._deployment,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "timeout": timeout_s,
        }

        def call() -> Any:
            # Up to two parameter adaptations for reasoning-class deployments, then
            # the request either succeeds or fails permanently.
            for _ in range(3):
                try:
                    return self._client.chat.completions.create(**params)
                except (
                    openai.APITimeoutError,
                    openai.APIConnectionError,
                    openai.RateLimitError,
                ) as exc:
                    raise TransientError(str(exc)) from exc
                except openai.APIStatusError as exc:
                    if exc.status_code >= 500:
                        raise TransientError(str(exc)) from exc
                    if exc.status_code == 400 and self._adapt_params(params, str(exc)):
                        continue
                    raise PermanentError(str(exc)) from exc
            raise PermanentError("azure request kept failing after parameter adaptation")

        response = with_retries(call, retries=self._retries)
        choice = response.choices[0]
        usage = response.usage
        return LLMResponse(
            text=choice.message.content or "",
            model=response.model or self._deployment,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cache_read_input_tokens=0,
            finish_reason=choice.finish_reason,
        )

    @staticmethod
    def _adapt_params(params: dict[str, Any], error_text: str) -> bool:
        """Mutate ``params`` for known unsupported-parameter 400s. True = retry."""
        if "max_tokens" in params and "max_tokens" in error_text:
            params["max_completion_tokens"] = params.pop("max_tokens")
            return True
        if "temperature" in params and "temperature" in error_text:
            params.pop("temperature")  # model only supports its default
            return True
        return False
