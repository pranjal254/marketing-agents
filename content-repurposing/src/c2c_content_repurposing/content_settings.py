"""Per-campaign content settings: how many variants of each asset to produce,
and how long each one should be.

Marketing asked for this because the shape of a campaign changes campaign to
campaign: one needs four LinkedIn posts and a short one-pager, the next needs
one post and a long FAQ. The composition config cannot answer that, and it is
read-only at runtime by design (kit hard rule 6), so the override lives here
instead: a versioned record in the context store, written by the Content Writer
between confirming the flagship and triggering the fan-out.

Three things this deliberately does NOT do.

It does not let a caller exceed the configured ceiling. ``maxVariantsPerAsset``
is governance, not preference, so a request for more is clamped and reported
rather than honoured quietly. The same goes for the word floor and ceiling.

It does not mutate config. Settings are a separate record keyed by campaign; the
config pack stays the default everything falls back to.

It does not overwrite history. Every save is a new version carrying who set it
and when, because "who asked for six emails" is a question people ask later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from c2c_content_repurposing.agent_config import ContentLimits, RepurposingConfig, WordRange


class AssetContentSetting(BaseModel):
    """What the writer asked for on one asset, already clamped to config."""

    model_config = ConfigDict(frozen=True)

    asset_id: str
    asset_type: str
    label: str = ""
    variants: int = Field(ge=1)
    min_words: int = Field(ge=1)
    max_words: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> AssetContentSetting:
        if self.max_words < self.min_words:
            raise ValueError(
                f"{self.asset_id}: max_words ({self.max_words}) is below "
                f"min_words ({self.min_words})"
            )
        return self


class ContentSettings(BaseModel):
    """One version of a campaign's settings, identity-stamped."""

    model_config = ConfigDict(frozen=True)

    campaign_id: str
    version: int = Field(ge=1)
    items: list[AssetContentSetting] = Field(default_factory=list)
    set_by: str | None = None
    set_by_role: str | None = None
    set_at: str = ""
    note: str | None = None
    # Populated when a request asked for more than config allows. Surfaced to
    # the writer so a clamp is visible rather than a silent correction.
    adjustments: list[str] = Field(default_factory=list)

    def for_asset(self, asset_id: str) -> AssetContentSetting | None:
        return next((i for i in self.items if i.asset_id == asset_id), None)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def default_setting(
    asset_id: str,
    asset_type: str,
    label: str,
    requested_variants: int,
    limits: ContentLimits,
) -> AssetContentSetting:
    """The starting point for one asset: the plan's volume and the config range."""
    word_range: WordRange = limits.word_range_for(asset_type)
    return AssetContentSetting(
        asset_id=asset_id,
        asset_type=asset_type,
        label=label,
        variants=_clamp(requested_variants, 1, limits.max_variants_per_asset),
        min_words=word_range.min_words,
        max_words=word_range.max_words,
    )


def defaults_for_checklist(
    campaign_id: str,
    checklist_items: list[Any],
    config: RepurposingConfig,
) -> ContentSettings:
    """Version 0 of a campaign's settings, derived from the approved plan.

    Version 0 is never stored: it is what the writer is shown before they change
    anything, and what generation falls back to if they never do.
    """
    limits = config.content_limits
    items = [
        default_setting(
            asset_id=str(getattr(item, "asset_id", "")),
            asset_type=str(getattr(item, "asset_type", "")),
            label=str(getattr(item, "label", "")),
            requested_variants=int(getattr(item, "volume", 1) or 1),
            limits=limits,
        )
        for item in checklist_items
    ]
    return ContentSettings(campaign_id=campaign_id, version=1, items=items, set_at=_now())


def apply_requests(
    campaign_id: str,
    version: int,
    requests: list[dict[str, Any]],
    baseline: ContentSettings,
    config: RepurposingConfig,
    *,
    set_by: str,
    set_by_role: str,
    note: str | None = None,
) -> ContentSettings:
    """Fold a writer's requested changes onto the baseline, clamped to config.

    Anything the request does not mention keeps its baseline value, so a partial
    update is safe. An asset the plan does not contain is ignored rather than
    invented. Every clamp is recorded in ``adjustments``.
    """
    limits = config.content_limits
    by_asset = {str(r.get("asset_id")): r for r in requests if r.get("asset_id")}
    adjustments: list[str] = []
    items: list[AssetContentSetting] = []

    for current in baseline.items:
        request = by_asset.pop(current.asset_id, None)
        if request is None:
            items.append(current)
            continue

        variants = int(request.get("variants", current.variants))
        clamped_variants = _clamp(variants, 1, limits.max_variants_per_asset)
        if clamped_variants != variants:
            adjustments.append(
                f"{current.label or current.asset_id}: {variants} variants requested, "
                f"set to {clamped_variants} (configured ceiling is "
                f"{limits.max_variants_per_asset})"
            )

        min_words = int(request.get("min_words", current.min_words))
        max_words = int(request.get("max_words", current.max_words))
        clamped_min = _clamp(min_words, limits.word_floor, limits.word_ceiling)
        clamped_max = _clamp(max_words, limits.word_floor, limits.word_ceiling)
        if clamped_min != min_words or clamped_max != max_words:
            adjustments.append(
                f"{current.label or current.asset_id}: word range {min_words} to "
                f"{max_words} adjusted to {clamped_min} to {clamped_max} (allowed "
                f"range is {limits.word_floor} to {limits.word_ceiling})"
            )
        if clamped_max < clamped_min:
            # An inverted range is a slip, not a decision; keep the writer's
            # minimum and lift the maximum to meet it rather than refusing.
            adjustments.append(
                f"{current.label or current.asset_id}: maximum was below the minimum, "
                f"raised to {clamped_min}"
            )
            clamped_max = clamped_min

        items.append(
            current.model_copy(
                update={
                    "variants": clamped_variants,
                    "min_words": clamped_min,
                    "max_words": clamped_max,
                }
            )
        )

    for unknown in sorted(by_asset):
        adjustments.append(f"{unknown}: not on the approved asset plan, ignored")

    return ContentSettings(
        campaign_id=campaign_id,
        version=version,
        items=items,
        set_by=set_by,
        set_by_role=set_by_role,
        set_at=_now(),
        note=note,
        adjustments=adjustments,
    )


def resolve(
    settings: ContentSettings | None,
    asset_id: str,
    asset_type: str,
    fallback_variants: int,
    config: RepurposingConfig,
) -> AssetContentSetting:
    """What generation should actually use for one asset.

    Falls back to config defaults when the campaign has no saved settings or the
    asset was added to the plan after they were saved.
    """
    if settings is not None:
        chosen = settings.for_asset(asset_id)
        if chosen is not None:
            return chosen
    return default_setting(
        asset_id, asset_type, "", fallback_variants, config.content_limits
    )


def word_count(text: str) -> int:
    return len(text.split())
