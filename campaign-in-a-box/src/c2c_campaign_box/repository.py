"""Step 5 (deterministic half): repository search + reuse-fitness scoring.

The repository is READ-ONLY to every agent (guardrail 3): the protocol exposes
search only. Fitness scores are deterministic (config weights) so every reuse
decision can cite evaluated candidates and scores (spec Explainability); the LLM
adds the decision rationale on top, never the scores.

Dev binding scans a local folder; assets may carry a ``<name>.meta.json`` sidecar
({"asset_type", "vertical", "business_unit", "topics": []}) — otherwise metadata is
inferred from path segments and filename tokens only. Nothing is ever guessed
beyond what the file system states.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from c2c_campaign_box.agent_config import FitnessWeights, OrchestratorConfig
from c2c_campaign_box.models import RepoCandidate


@dataclass(frozen=True)
class RepoQuery:
    business_unit: str
    vertical: str
    topic: str
    asset_type: str


@dataclass(frozen=True)
class AssetMeta:
    asset_type: str | None
    vertical: str | None
    business_unit: str | None
    topics: list[str]


def _opt_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


class RepositoryIndex(Protocol):
    """Read-only search surface — no write method exists anywhere."""

    def available(self) -> bool: ...

    def search(self, query: RepoQuery) -> list[RepoCandidate]: ...


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2}


def score_candidate(
    weights: FitnessWeights,
    query: RepoQuery,
    *,
    asset_type: str | None,
    vertical: str | None,
    business_unit: str | None,
    title: str,
    topics: list[str],
) -> tuple[float, dict[str, float]]:
    """Deterministic fitness score in [0,1] with a per-dimension breakdown."""
    breakdown: dict[str, float] = {}
    breakdown["asset_type"] = weights.asset_type if asset_type == query.asset_type else 0.0
    breakdown["vertical"] = (
        weights.vertical if vertical and vertical.lower() == query.vertical.lower() else 0.0
    )
    breakdown["business_unit"] = (
        weights.business_unit
        if business_unit and business_unit.lower() == query.business_unit.lower()
        else 0.0
    )
    query_tokens = _tokens(query.topic)
    asset_tokens = _tokens(title) | {t.lower() for t in topics}
    overlap = len(query_tokens & asset_tokens) / len(query_tokens) if query_tokens else 0.0
    breakdown["topic"] = round(weights.topic * overlap, 4)
    total = round(sum(breakdown.values()), 4)
    return min(total, 1.0), breakdown


class LocalRepositoryIndex:
    """Dev/test binding over a local read-only folder tree."""

    def __init__(self, root: str, weights: FitnessWeights) -> None:
        self._root = Path(root)
        self._weights = weights

    def available(self) -> bool:
        return self._root.is_dir()

    def search(self, query: RepoQuery) -> list[RepoCandidate]:
        if not self.available():
            return []
        candidates: list[RepoCandidate] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file() or path.name.endswith(".meta.json"):
                continue
            meta = self._load_meta(path)
            score, breakdown = score_candidate(
                self._weights,
                query,
                asset_type=meta.asset_type,
                vertical=meta.vertical,
                business_unit=meta.business_unit,
                title=path.stem,
                topics=meta.topics,
            )
            if score <= 0.0:
                continue
            candidates.append(
                RepoCandidate(
                    asset_ref=str(path),
                    title=path.stem,
                    asset_type=meta.asset_type,
                    vertical=meta.vertical,
                    business_unit=meta.business_unit,
                    fitness_score=score,
                    score_breakdown=breakdown,
                )
            )
        candidates.sort(key=lambda c: c.fitness_score, reverse=True)
        return candidates[:5]

    def _load_meta(self, path: Path) -> AssetMeta:
        sidecar = path.with_name(path.name + ".meta.json")
        if sidecar.is_file():
            try:
                loaded = json.loads(sidecar.read_text("utf-8"))
                if isinstance(loaded, dict):
                    return AssetMeta(
                        asset_type=_opt_str(loaded.get("asset_type")),
                        vertical=_opt_str(loaded.get("vertical")),
                        business_unit=_opt_str(loaded.get("business_unit")),
                        topics=[str(t) for t in loaded.get("topics", []) or []],
                    )
            except (OSError, json.JSONDecodeError):
                pass
        # Inferred metadata: path segments only — never invented values.
        parts = [p.lower() for p in path.relative_to(self._root).parts[:-1]]
        return AssetMeta(asset_type=None, vertical=None, business_unit=None, topics=parts)


def search_all_types(
    index: RepositoryIndex,
    config: OrchestratorConfig,
    *,
    business_unit: str,
    vertical: str,
    topic: str,
) -> tuple[dict[str, list[RepoCandidate]], bool]:
    """One search per composition asset type. Returns (candidates by type,
    search_performed). ``search_performed=False`` → every asset becomes
    create + reuse_check_pending (spec fallback) — never a silent skip."""
    if not index.available():
        return {c.asset_type: [] for c in config.composition}, False
    results: dict[str, list[RepoCandidate]] = {}
    for item in config.composition:
        results[item.asset_type] = index.search(
            RepoQuery(
                business_unit=business_unit,
                vertical=vertical,
                topic=topic,
                asset_type=item.asset_type,
            )
        )
    return results, True
