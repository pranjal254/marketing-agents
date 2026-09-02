"""Multi-model fleet pricing: cost is keyed to the model that actually answered."""

from __future__ import annotations

from shiftai_shared.config import DEFAULT_RATE_CARD, SharedSettings, runtime_rate_card
from shiftai_shared.telemetry.envelope import rate_card_cost, response_cost


def test_prefix_match_prices_versioned_model_ids() -> None:
    # Providers append version/date suffixes; the base entry must still match.
    exact = rate_card_cost("claude-opus-5", 1_000_000, 0)
    suffixed = rate_card_cost("claude-opus-5-20260115", 1_000_000, 0)
    assert exact == suffixed == 5.0


def test_unknown_model_has_no_cost_never_guessed() -> None:
    assert rate_card_cost("gpt-5.4-nano-2026", 1_000_000, 1_000_000) is None


def test_response_cost_prefers_actual_model_over_target() -> None:
    card = {**DEFAULT_RATE_CARD, "gpt-5.4-nano": (0.10, 0.40)}
    # Dev run: Azure answered — priced at Azure rates, not Claude's.
    dev = response_cost("gpt-5.4-nano-2026-01", "claude-opus-5", 1_000_000, 1_000_000,
                        rate_card=card)
    assert dev == 0.50
    # Production: Claude answered — Claude rates.
    prod = response_cost("claude-opus-5", "claude-opus-5", 1_000_000, 1_000_000,
                         rate_card=card)
    assert prod == 30.0


def test_response_cost_falls_back_to_target_model_when_actual_unpriced() -> None:
    # No contracted Azure rate configured → modeled at the production target's card.
    modeled = response_cost("gpt-5.4-nano-2026-01", "claude-opus-5", 1_000_000, 0)
    assert modeled == 5.0


def test_runtime_rate_card_adds_azure_deployment_rates() -> None:
    settings = SharedSettings(
        _env_file=None,
        AZURE_OPENAI_DEPLOYMENT="gpt-5.4-nano",
        AZURE_OPENAI_RATE_INPUT=0.10,
        AZURE_OPENAI_RATE_OUTPUT=0.40,
    )
    card = runtime_rate_card(settings)
    assert card["gpt-5.4-nano"] == (0.10, 0.40)
    assert card["claude-opus-5"] == DEFAULT_RATE_CARD["claude-opus-5"]


def test_runtime_rate_card_without_rates_is_default() -> None:
    settings = SharedSettings(_env_file=None, AZURE_OPENAI_DEPLOYMENT="gpt-5.4-nano")
    assert runtime_rate_card(settings) == DEFAULT_RATE_CARD


def test_blank_rate_env_treated_as_unset() -> None:
    settings = SharedSettings(
        _env_file=None, AZURE_OPENAI_RATE_INPUT="", AZURE_OPENAI_RATE_OUTPUT=""
    )
    assert settings.azure_openai_rate_input is None
    assert settings.azure_openai_rate_output is None
