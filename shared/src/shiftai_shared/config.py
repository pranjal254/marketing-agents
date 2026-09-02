"""Environment-only settings. Secrets never live in code, prompts, telemetry, or logs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # Contracted USD price per 1M tokens for the Azure deployment. When set, dev
    # runs are costed at the Azure model's own rates; when unset, telemetry falls
    # back to the production target model's rate card (a modeled cost, never a
    # guessed Azure price).
    azure_openai_rate_input: float | None = Field(default=None, alias="AZURE_OPENAI_RATE_INPUT")
    azure_openai_rate_output: float | None = Field(default=None, alias="AZURE_OPENAI_RATE_OUTPUT")

    @field_validator("azure_openai_rate_input", "azure_openai_rate_output", mode="before")
    @classmethod
    def _blank_rate_is_unset(cls, value: object) -> object:
        return None if value == "" else value

    # Microsoft Graph (client-credential flow)
    graph_tenant_id: str | None = Field(default=None, alias="GRAPH_TENANT_ID")
    graph_client_id: str | None = Field(default=None, alias="GRAPH_CLIENT_ID")
    graph_client_secret: SecretStr | None = Field(default=None, alias="GRAPH_CLIENT_SECRET")

    # SemRush (Agent 2 competitive/market intel). No key → intel-library-only
    # fallback mode, flagged (spec fallback) — never a hard failure.
    semrush_api_key: SecretStr | None = Field(default=None, alias="SEMRUSH_API_KEY")
    semrush_database: str = Field(default="us", alias="SEMRUSH_DATABASE")


def load_settings() -> SharedSettings:
    return SharedSettings()


def runtime_rate_card(settings: SharedSettings) -> dict[str, tuple[float, float]]:
    """The fleet rate card for this process: the default Claude card plus the Azure
    deployment's contracted rates when configured (keyed by deployment name; model
    ids with version suffixes match by prefix at lookup time)."""
    card = dict(DEFAULT_RATE_CARD)
    if (
        settings.azure_openai_deployment
        and settings.azure_openai_rate_input is not None
        and settings.azure_openai_rate_output is not None
    ):
        card[settings.azure_openai_deployment] = (
            settings.azure_openai_rate_input,
            settings.azure_openai_rate_output,
        )
    return card
