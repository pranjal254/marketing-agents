"""Provider tests with mocked SDKs — no live API calls anywhere."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from shiftai_shared.config import SharedSettings
from shiftai_shared.llm import MockLLMProvider, SystemBlock, build_provider
from shiftai_shared.llm.anthropic_client import AnthropicClient
from shiftai_shared.llm.azure_openai_client import AzureOpenAIClient


def _anthropic_response() -> MagicMock:
    response = MagicMock()
    part = MagicMock()
    part.type = "text"
    part.text = '{"ok": true}'
    response.content = [part]
    response.model = "claude-sonnet-5"
    response.stop_reason = "end_turn"
    response.usage.input_tokens = 100
    response.usage.output_tokens = 20
    response.usage.cache_read_input_tokens = 80
    return response


def test_anthropic_payload_has_cache_control() -> None:
    with patch("anthropic.Anthropic") as anthropic_cls:
        client_mock = anthropic_cls.return_value
        client_mock.messages.create.return_value = _anthropic_response()
        client = AnthropicClient(api_key="test-key")
        result = client.complete(
            system=[
                SystemBlock(text="stable system prompt"),
                SystemBlock(text="volatile", cache=False),
            ],
            user="hello",
            model="claude-sonnet-5",
            max_tokens=1000,
        )
    kwargs: dict[str, Any] = client_mock.messages.create.call_args.kwargs
    system_param = kwargs["system"]
    assert system_param[0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in system_param[1]
    assert kwargs["model"] == "claude-sonnet-5"
    assert result.cache_read_input_tokens == 80
    assert result.text == '{"ok": true}'


def test_azure_maps_usage_and_uses_deployment() -> None:
    with patch("openai.AzureOpenAI") as azure_cls:
        client_mock = azure_cls.return_value
        response = MagicMock()
        choice = MagicMock()
        choice.message.content = "hi"
        choice.finish_reason = "stop"
        response.choices = [choice]
        response.model = "gpt-4o"
        response.usage.prompt_tokens = 50
        response.usage.completion_tokens = 5
        client_mock.chat.completions.create.return_value = response

        client = AzureOpenAIClient(
            endpoint="https://example.openai.azure.com",
            api_key="k",
            deployment="gpt-4o-dev",
        )
        result = client.complete(
            system=[SystemBlock(text="sys")],
            user="u",
            model="claude-sonnet-5",  # routed model id is recorded, deployment serves it
            max_tokens=100,
        )
    kwargs = client_mock.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-4o-dev"
    assert kwargs["messages"][0]["role"] == "system"
    assert result.input_tokens == 50 and result.output_tokens == 5
    assert result.model == "gpt-4o"


def test_azure_adapts_to_reasoning_model_params() -> None:
    """gpt-5.x / o-series deployments: max_tokens→max_completion_tokens, temperature
    dropped — adapted transparently on the API's unsupported-parameter 400s."""
    import httpx
    import openai

    def bad_request(message: str) -> openai.BadRequestError:
        request = httpx.Request("POST", "https://example.openai.azure.com")
        return openai.BadRequestError(
            message, response=httpx.Response(400, request=request), body=None
        )

    with patch("openai.AzureOpenAI") as azure_cls:
        client_mock = azure_cls.return_value
        ok = MagicMock()
        choice = MagicMock()
        choice.message.content = "ok"
        choice.finish_reason = "stop"
        ok.choices = [choice]
        ok.model = "gpt-5.4-nano"
        ok.usage.prompt_tokens = 10
        ok.usage.completion_tokens = 2
        client_mock.chat.completions.create.side_effect = [
            bad_request("Unsupported parameter: 'max_tokens' is not supported with this model."),
            bad_request("Unsupported value: 'temperature' does not support 0.0 with this model."),
            ok,
        ]
        client = AzureOpenAIClient(
            endpoint="https://example.openai.azure.com", api_key="k", deployment="gpt-5.4-nano"
        )
        result = client.complete(
            system=[SystemBlock(text="s")], user="u", model="claude-sonnet-5", max_tokens=100
        )
    final_kwargs = client_mock.chat.completions.create.call_args.kwargs
    assert final_kwargs["max_completion_tokens"] == 100
    assert "max_tokens" not in final_kwargs
    assert "temperature" not in final_kwargs
    assert result.text == "ok"


def test_build_provider_mock_and_azure_validation() -> None:
    # _env_file=None keeps the test hermetic: a developer's local .env (real Azure
    # values) must not change unit-test behavior.
    settings = SharedSettings(_env_file=None, LLM_PROVIDER="mock")
    assert isinstance(build_provider(settings), MockLLMProvider)
    with pytest.raises(ValueError, match="azure_openai provider requires"):
        build_provider(SharedSettings(_env_file=None, LLM_PROVIDER="azure_openai"))


def test_mock_provider_scripting() -> None:
    provider = MockLLMProvider(
        default="{}",
        script=[(lambda u: "trigger" in u, '{"hit": 1}')],
    )
    out = provider.complete(
        system=[SystemBlock(text="s")], user="has trigger word", model="m", max_tokens=10
    )
    assert out.text == '{"hit": 1}'
    assert provider.calls[0]["model"] == "m"
