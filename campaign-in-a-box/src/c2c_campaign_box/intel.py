"""Step 2: sourced competitive/market intel — SemRush + the intel library.

Every signal carries a source URI and retrieval timestamp. SemRush failure or a
missing key engages intel-library-only fallback mode, flagged — never silent, never
a hard failure (spec Fallback). No signal is ever fabricated: what cannot be
sourced simply is not in the bundle.

AEO/LLM-citation data (spec step 2) has no stable public SemRush endpoint at build
time — deliberately absent, recorded as an open item in agent-spec.md.
"""

from __future__ import annotations

from typing import Protocol

from shiftai_shared.semrush import SemrushClient, TopicSignal
from shiftai_shared.telemetry.emitter import utc_now_iso

from c2c_campaign_box.models import IntelBundle, IntelSignal
from c2c_campaign_box.workspace import CampaignWorkspace

INTEL_LIBRARY_PATH = "02-Reference/intel-library"
_TEXT_SUFFIXES = (".md", ".txt")
_EXCERPT_CHARS = 400


class IntelSource(Protocol):
    """Live search source (SemRush in production; mocks in tests)."""

    def topic_signal(self, topic: str) -> TopicSignal: ...


def semrush_source(client: SemrushClient) -> IntelSource:
    return client


def _semrush_signals(signal: TopicSignal) -> list[IntelSignal]:
    out: list[IntelSignal] = []
    for i, kw in enumerate(signal.keywords):
        out.append(
            IntelSignal(
                signal_id=f"semrush-kw-{i}",
                origin="semrush",
                kind="keyword",
                summary=(
                    f"Keyword '{kw.phrase}': volume {kw.volume}, CPC {kw.cpc}, "
                    f"competition {kw.competition}"
                ),
                source_uri=kw.source_uri,
                retrieved_at=kw.retrieved_at,
                data={"volume": kw.volume, "cpc": kw.cpc, "competition": kw.competition},
            )
        )
    for i, kw in enumerate(signal.related):
        out.append(
            IntelSignal(
                signal_id=f"semrush-rel-{i}",
                origin="semrush",
                kind="related_keyword",
                summary=f"Related keyword '{kw.phrase}': volume {kw.volume}",
                source_uri=kw.source_uri,
                retrieved_at=kw.retrieved_at,
                data={"volume": kw.volume},
            )
        )
    for i, org in enumerate(signal.organic):
        out.append(
            IntelSignal(
                signal_id=f"semrush-org-{i}",
                origin="semrush",
                kind="organic_result",
                summary=f"'{org.domain}' ranks organically for the topic ({org.url})",
                source_uri=org.source_uri,
                retrieved_at=org.retrieved_at,
                data={"domain": org.domain, "url": org.url},
            )
        )
    return out


def _library_signals(workspace: CampaignWorkspace) -> list[IntelSignal]:
    ts = utc_now_iso()
    out: list[IntelSignal] = []
    for i, file in enumerate(workspace.list_files(INTEL_LIBRARY_PATH)):
        excerpt = ""
        if file.name.lower().endswith(_TEXT_SUFFIXES):
            try:
                excerpt = workspace.download(file.ref).decode("utf-8", "replace")[:_EXCERPT_CHARS]
            except Exception:
                excerpt = ""  # unreadable file stays listed by name only
        out.append(
            IntelSignal(
                signal_id=f"intel-file-{i}",
                origin="intel_library",
                kind="file",
                summary=f"Curated intel file '{file.name}'" + (f": {excerpt}" if excerpt else ""),
                source_uri=file.ref,
                retrieved_at=ts,
                data={"filename": file.name},
            )
        )
    return out


def gather_intel(
    topic: str,
    workspace: CampaignWorkspace,
    source: IntelSource | None,
) -> IntelBundle:
    """Assemble the sourced intel bundle. ``source=None`` (no key) or a SemRush
    failure → intel-library-only mode with the failure reason recorded."""
    signals = _library_signals(workspace)
    if source is None:
        return IntelBundle(
            topic=topic,
            mode="intel_library_only",
            signals=signals,
            semrush_failure="no SemRush API key configured",
        )
    try:
        semrush = _semrush_signals(source.topic_signal(topic))
    except Exception as exc:  # quota / transport / API errors → documented fallback
        return IntelBundle(
            topic=topic,
            mode="intel_library_only",
            signals=signals,
            semrush_failure=f"{type(exc).__name__}: {exc}",
        )
    return IntelBundle(
        topic=topic, mode="semrush_plus_library", signals=semrush + signals, semrush_failure=None
    )
