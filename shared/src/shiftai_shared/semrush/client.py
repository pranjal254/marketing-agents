"""SemRush Analytics API client (spec Agent 2: keyword landscape + competitor
visibility for the brief topic).

Real API shape only — SemRush Analytics v3 report endpoints returning
semicolon-separated CSV (https://api.semrush.com/). AEO/LLM-citation data has no
stable public endpoint at build time, so it is deliberately NOT implemented: those
data points are absent, never fabricated (spec open item, agent-spec §conflicts).

Resilience per spec: 3 retries, exponential backoff from 2s, 60s timeout per call.
Quota exhaustion raises ``SemrushQuotaError`` so the caller can switch to
intel-library-only mode (spec fallback) — flagged, never silent.

The API key is a query parameter; provenance URIs returned to callers are
``semrush://`` pseudo-URIs that never contain the key (secrets never reach
telemetry or documents).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from shiftai_shared.resilience import PermanentError, TransientError, with_retries

SEMRUSH_BASE = "https://api.semrush.com/"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# SemRush v3 error codes that mean "units/quota exhausted" (documented API errors).
_QUOTA_ERROR_CODES = {"131", "132"}


class SemrushError(PermanentError):
    """A non-retryable SemRush failure (bad request, auth, unknown report)."""


class SemrushQuotaError(SemrushError):
    """API units exhausted — callers fall back to intel-library-only mode."""


@dataclass(frozen=True)
class KeywordStat:
    """One row of a keyword report (phrase_all / phrase_related)."""

    phrase: str
    volume: int | None
    cpc: float | None
    competition: float | None
    results: int | None
    source_uri: str
    retrieved_at: str


@dataclass(frozen=True)
class OrganicResult:
    """One row of phrase_organic — a domain visible for the topic keyword."""

    domain: str
    url: str
    source_uri: str
    retrieved_at: str


@dataclass(frozen=True)
class TopicSignal:
    """Everything the client can source for one topic, with per-row provenance."""

    topic: str
    database: str
    keywords: list[KeywordStat]
    related: list[KeywordStat]
    organic: list[OrganicResult]


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _pseudo_uri(report_type: str, params: dict[str, str]) -> str:
    """Provenance URI without the API key."""
    clean = {k: v for k, v in params.items() if k != "key"}
    return f"semrush://{report_type}?{urlencode(clean)}"


def _parse_int(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_float(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


class SemrushClient:
    """Thin, testable client. Inject ``http`` in tests — no live calls ever in CI."""

    def __init__(
        self,
        api_key: str,
        *,
        database: str = "us",
        http: httpx.Client | None = None,
        timeout_s: float = 60.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self._database = database
        self._http = http or httpx.Client(timeout=timeout_s)
        self._timeout_s = timeout_s
        self._retries = retries
        self._sleep = sleep

    # ------------------------------------------------------------ raw transport

    def _fetch_report(self, report_type: str, extra: dict[str, str]) -> tuple[str, str]:
        """Returns (csv_text, provenance_uri). Raises typed errors."""
        params: dict[str, str] = {
            "type": report_type,
            "key": self._api_key,
            "database": self._database,
            **extra,
        }
        uri = _pseudo_uri(report_type, params)

        def call() -> str:
            try:
                response = self._http.get(
                    SEMRUSH_BASE, params=params, timeout=self._timeout_s
                )
            except httpx.HTTPError as exc:
                raise TransientError(f"semrush transport: {exc}") from exc
            if response.status_code in _RETRYABLE_STATUS:
                raise TransientError(f"semrush HTTP {response.status_code}")
            if response.status_code != 200:
                raise SemrushError(f"semrush HTTP {response.status_code}")
            return response.text

        text = with_retries(call, retries=self._retries, sleep=self._sleep)
        stripped = text.strip()
        if stripped.startswith("ERROR"):
            # v3 error format: "ERROR <code> :: <message>"
            parts = stripped.split("::")
            code = parts[0].replace("ERROR", "").strip()
            if code in _QUOTA_ERROR_CODES:
                raise SemrushQuotaError(stripped)
            raise SemrushError(stripped)
        return text, uri

    @staticmethod
    def _rows(csv_text: str) -> list[dict[str, str]]:
        lines = [ln for ln in csv_text.strip().splitlines() if ln.strip()]
        if len(lines) < 2:
            return []
        header = [h.strip() for h in lines[0].split(";")]
        out: list[dict[str, str]] = []
        for line in lines[1:]:
            values = [v.strip() for v in line.split(";")]
            out.append(dict(zip(header, values, strict=False)))
        return out

    # ---------------------------------------------------------------- reports

    def keyword_overview(self, phrase: str) -> list[KeywordStat]:
        """phrase_all: keyword landscape for the topic phrase."""
        text, uri = self._fetch_report(
            "phrase_all", {"phrase": phrase, "export_columns": "Ph,Nq,Cp,Co,Nr"}
        )
        ts = _now_iso()
        return [
            KeywordStat(
                phrase=row.get("Keyword", row.get("Ph", phrase)),
                volume=_parse_int(row.get("Search Volume", row.get("Nq", ""))),
                cpc=_parse_float(row.get("CPC", row.get("Cp", ""))),
                competition=_parse_float(row.get("Competition", row.get("Co", ""))),
                results=_parse_int(row.get("Number of Results", row.get("Nr", ""))),
                source_uri=uri,
                retrieved_at=ts,
            )
            for row in self._rows(text)
        ]

    def related_keywords(self, phrase: str, limit: int = 20) -> list[KeywordStat]:
        text, uri = self._fetch_report(
            "phrase_related",
            {"phrase": phrase, "export_columns": "Ph,Nq,Cp,Co,Nr", "display_limit": str(limit)},
        )
        ts = _now_iso()
        return [
            KeywordStat(
                phrase=row.get("Keyword", row.get("Ph", "")),
                volume=_parse_int(row.get("Search Volume", row.get("Nq", ""))),
                cpc=_parse_float(row.get("CPC", row.get("Cp", ""))),
                competition=_parse_float(row.get("Competition", row.get("Co", ""))),
                results=_parse_int(row.get("Number of Results", row.get("Nr", ""))),
                source_uri=uri,
                retrieved_at=ts,
            )
            for row in self._rows(text)
        ]

    def organic_results(self, phrase: str, limit: int = 20) -> list[OrganicResult]:
        """phrase_organic: domains ranking for the phrase — competitor visibility."""
        text, uri = self._fetch_report(
            "phrase_organic",
            {"phrase": phrase, "export_columns": "Dn,Ur", "display_limit": str(limit)},
        )
        ts = _now_iso()
        return [
            OrganicResult(
                domain=row.get("Domain", row.get("Dn", "")),
                url=row.get("Url", row.get("Ur", "")),
                source_uri=uri,
                retrieved_at=ts,
            )
            for row in self._rows(text)
        ]

    def topic_signal(self, topic: str) -> TopicSignal:
        """The Agent 2 step-2 bundle: keyword landscape + competitor visibility."""
        return TopicSignal(
            topic=topic,
            database=self._database,
            keywords=self.keyword_overview(topic),
            related=self.related_keywords(topic),
            organic=self.organic_results(topic),
        )
