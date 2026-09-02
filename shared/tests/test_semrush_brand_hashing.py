"""SemRush client (mock transport only — no live calls), brand rules pack, hashing."""

from __future__ import annotations

import httpx
import pytest

from shiftai_shared.brand import (
    BRAND_RULES_VERSION,
    brand_prompt_block,
    lint_text,
    load_brand_rules,
)
from shiftai_shared.hashing import hashes_match, sha256_hex
from shiftai_shared.resilience import TransientError
from shiftai_shared.semrush import SemrushClient, SemrushError, SemrushQuotaError

# ---------------------------------------------------------------------- semrush


def make_client(handler: httpx.MockTransport) -> SemrushClient:
    return SemrushClient(
        "test-key-never-real",
        database="us",
        http=httpx.Client(transport=handler),
        sleep=lambda _s: None,
    )


def transport_returning(text: str, status: int = 200) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, text=text))


def test_keyword_overview_parses_csv_rows() -> None:
    csv = (
        "Keyword;Search Volume;CPC;Competition;Number of Results\n"
        "erp modernization;1300;12.50;0.45;90000000\n"
    )
    client = make_client(transport_returning(csv))
    stats = client.keyword_overview("erp modernization")
    assert len(stats) == 1
    assert stats[0].phrase == "erp modernization"
    assert stats[0].volume == 1300
    assert stats[0].cpc == 12.50
    assert stats[0].source_uri.startswith("semrush://phrase_all?")


def test_provenance_uri_never_contains_api_key() -> None:
    csv = "Keyword;Search Volume;CPC;Competition;Number of Results\nx;1;1;0.1;10\n"
    client = make_client(transport_returning(csv))
    stats = client.keyword_overview("x")
    assert "test-key-never-real" not in stats[0].source_uri
    assert "key=" not in stats[0].source_uri


def test_quota_error_raises_typed_exception() -> None:
    client = make_client(transport_returning("ERROR 131 :: LIMIT EXCEEDED"))
    with pytest.raises(SemrushQuotaError):
        client.keyword_overview("x")


def test_api_error_raises_permanent_error() -> None:
    client = make_client(transport_returning("ERROR 120 :: WRONG KEY"))
    with pytest.raises(SemrushError):
        client.keyword_overview("x")


def test_transient_http_status_retries_then_raises() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = make_client(httpx.MockTransport(handler))
    with pytest.raises(TransientError):
        client.keyword_overview("x")
    assert calls["n"] == 4  # initial + 3 retries (spec retry policy)


def test_retry_recovers_on_second_attempt() -> None:
    calls = {"n": 0}
    csv = "Keyword;Search Volume;CPC;Competition;Number of Results\nx;5;0.5;0.2;100\n"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429)
        return httpx.Response(200, text=csv)

    client = make_client(httpx.MockTransport(handler))
    assert client.keyword_overview("x")[0].volume == 5


def test_topic_signal_bundles_three_reports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        rtype = request.url.params.get("type")
        if rtype == "phrase_organic":
            return httpx.Response(200, text="Domain;Url\ncompetitor.com;https://competitor.com/x\n")
        return httpx.Response(
            200, text="Keyword;Search Volume;CPC;Competition;Number of Results\nk;9;1;0.3;10\n"
        )

    client = make_client(httpx.MockTransport(handler))
    signal = client.topic_signal("k")
    assert signal.keywords and signal.related and signal.organic
    assert signal.organic[0].domain == "competitor.com"


# ------------------------------------------------------------------ brand rules


def test_brand_rules_load_and_version() -> None:
    rules = load_brand_rules()
    assert rules.version == BRAND_RULES_VERSION
    assert len(rules.voice) == 4
    assert len(rules.personas) == 4
    assert rules.word_choice.banned_terms == []  # placeholder until Marketing's list
    assert "blog" in rules.playbooks


def test_lint_flags_shiftai_spacing_and_copilot_onprem() -> None:
    rules = load_brand_rules()
    findings = lint_text("Shift AI brings Copilot to on-premise ERP.", rules)
    ids = {f.rule_id for f in findings}
    assert "shiftai_one_word" in ids
    assert "copilot_d365_cloud_only" in ids
    assert all(f.severity == "error" for f in findings if f.rule_id in ids)


def test_lint_flags_bc_fo_mix_and_urgency() -> None:
    rules = load_brand_rules()
    findings = lint_text("Business Central and F&O together — act now!", rules)
    ids = {f.rule_id for f in findings}
    assert "bc_fo_independent" in ids
    assert "urgency_fear" in ids


def test_lint_warns_on_overuse_words_and_passes_clean_text() -> None:
    rules = load_brand_rules()
    warned = lint_text("We seamlessly leverage synergies.", rules)
    assert {f.term for f in warned} >= {"seamlessly", "leverage"}
    assert all(f.severity == "warning" for f in warned)
    clean = "ShiftAI helps manufacturers modernize ERP with measurable outcomes."
    assert lint_text(clean, rules) == []


def test_brand_prompt_block_is_stable_and_complete() -> None:
    rules = load_brand_rules()
    block = brand_prompt_block(rules)
    assert block == brand_prompt_block(rules)  # deterministic → cacheable
    assert "Authoritative (Not Arrogant)" in block
    assert "ShiftAI" in block


# ---------------------------------------------------------------------- hashing


def test_sha256_hex_and_match() -> None:
    digest = sha256_hex(b"final asset bytes")
    assert len(digest) == 64
    assert hashes_match(digest, b"final asset bytes")
    assert not hashes_match(digest, b"edited after packaging")
