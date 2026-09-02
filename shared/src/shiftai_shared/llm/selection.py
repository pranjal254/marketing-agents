"""Provider selection from environment configuration — nothing upstream of this
function knows or cares which provider is active (kit build spec §5.1)."""

from __future__ import annotations

from shiftai_shared.config import SharedSettings
from shiftai_shared.llm.provider import LLMProvider, MockLLMProvider


def build_provider(settings: SharedSettings) -> LLMProvider:
    if settings.llm_provider == "anthropic":
        from shiftai_shared.llm.anthropic_client import AnthropicClient

        key = settings.anthropic_api_key
        return AnthropicClient(api_key=key.get_secret_value() if key else None)
    if settings.llm_provider == "azure_openai":
        from shiftai_shared.llm.azure_openai_client import AzureOpenAIClient

        if not (
            settings.azure_openai_endpoint
            and settings.azure_openai_api_key
            and settings.azure_openai_deployment
        ):
            raise ValueError(
                "azure_openai provider requires AZURE_OPENAI_ENDPOINT, "
                "AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT"
            )
        return AzureOpenAIClient(
            endpoint=settings.azure_openai_endpoint,
            api_key=settings.azure_openai_api_key.get_secret_value(),
            deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
        )
    return MockLLMProvider()
