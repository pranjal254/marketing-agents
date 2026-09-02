"""Environment-only settings. Secrets never live in code, prompts, telemetry, or logs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["production", "staging", "dev"]
ProviderName = Literal["anthropic", "azure_openai", "mock"]

# USD per 1M tokens (input, output) — rate card, config-overridable.
DEFAULT_RATE_CARD: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
# Cached input reads are billed at ~10% of the input rate.
CACHE_READ_INPUT_RATE = 0.1


class SharedSettings(BaseSettings):
    """Process-wide settings from environment variables, with a local git-ignored
    `.env` as dev fallback (template: `Agents/.env.example`). Real environment
    variables always take precedence over `.env` values. Secrets never live in code
    or in committed files."""

    model_config = SettingsConfigDict(
        # Searched relative to the working directory, so runs from the repo root or
        # from any agent package directory find the root .env. Missing files are
        # ignored; tests pass _env_file=None to stay hermetic.
        env_file=(".env", "../.env", "../../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment / telemetry identity
    shiftai_environment: Environment = Field(default="dev", alias="SHIFTAI_ENVIRONMENT")
    shiftai_tenant_id: str = Field(default="levelshift-internal", alias="SHIFTAI_TENANT_ID")
    telemetry_sink_path: str = Field(default="telemetry-out.jsonl", alias="TELEMETRY_SINK_PATH")
    context_store_path: str = Field(default="context-store.sqlite", alias="CONTEXT_STORE_PATH")

    # LLM provider selection: production uses anthropic; dev may use azure_openai or mock.
    llm_provider: ProviderName = Field(default="anthropic", alias="LLM_PROVIDER")

    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    azure_openai_endpoint: str | None = Field(default=None, alias="AZURE_OPENAI_ENDPOINT")
    azure_openai_api_key: SecretStr | None = Field(default=None, alias="AZURE_OPENAI_API_KEY")
    azure_openai_deployment: str | None = Field(default=None, alias="AZURE_OPENAI_DEPLOYMENT")
    azure_openai_api_version: str = Field(default="2024-06-01", alias="AZURE_OPENAI_API_VERSION")

    # Microsoft Graph (client-credential flow)
    graph_tenant_id: str | None = Field(default=None, alias="GRAPH_TENANT_ID")
    graph_client_id: str | None = Field(default=None, alias="GRAPH_CLIENT_ID")
    graph_client_secret: SecretStr | None = Field(default=None, alias="GRAPH_CLIENT_SECRET")


def load_settings() -> SharedSettings:
    return SharedSettings()
