"""Derivative fan-out planning + the batch execution seam (spec steps 6-7).

Jobs come ONLY from the approved asset checklist (create/adapt items with a
channel recipe; reuse assets and the flagship itself are skipped) — generating
anything else is impossible by construction (spec guardrail 4). Volumes come from
the checklist item (Agent 2's composition is the authority).

Execution seam: ``run_fanout_jobs`` walks independent per-derivative calls. In
dev they run sequentially (deterministic telemetry ordering, SQLite-friendly);
the production Anthropic binding replaces this walker with the Message Batches
API (50% off — spec Cross-Agent Standard A) behind the same signature. Nothing
outside this function knows how the calls are executed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from c2c_campaign_box.models import AssetChecklist

from c2c_content_repurposing.agent_config import ChannelRecipe, RepurposingConfig


@dataclass(frozen=True)
class DerivativeJob:
    asset_id: str
    asset_type: str
    recipe: ChannelRecipe
    volume: int


def build_fanout_jobs(
    checklist: AssetChecklist, config: RepurposingConfig
) -> tuple[list[DerivativeJob], list[str]]:
    """Returns (jobs, skipped asset_ids). Skips: the flagship (already drafted),
    reuse decisions (nothing to draft — the repository asset is reused as-is),
    and asset types without a configured channel recipe."""
    jobs: list[DerivativeJob] = []
    skipped: list[str] = []
    for item in checklist.items:
        if item.asset_type == config.flagship_asset_type:
            continue
        if item.decision == "reuse":
            skipped.append(item.asset_id)
            continue
        recipe = config.recipe_for(item.asset_type)
        if recipe is None:
            skipped.append(item.asset_id)
            continue
        jobs.append(
            DerivativeJob(
                asset_id=item.asset_id,
                asset_type=item.asset_type,
                recipe=recipe,
                volume=max(item.volume, 1),
            )
        )
    return jobs, skipped


def run_fanout_jobs[T](jobs: list[DerivativeJob], worker: Callable[[DerivativeJob], T]) -> list[T]:
    """The execution seam. Dev: sequential independent calls. Prod (Anthropic):
    the Message Batches API binding submits all jobs in one batch and collects
    results — same inputs, same outputs, no caller change."""
    return [worker(job) for job in jobs]
