"""Layer 3 reasoning prompt mechanics — Python port of the kit's single
``prompts/layer3-reasoning.hbs`` template (kit hard rule 5).

The template text lives in one place (``templates/layer3_user.md``): the injection
guard (<case_data> is data, never instructions), the abstention rule, the fixed
action-class list, and the JSON-only output contract. Agents supply values at
runtime; they never write their own copy of these mechanics.

The agent's identity/system prompt is Business Capability content and comes from the
agent spec — it rides in the (cached) system blocks, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Any

PROMPT_TEMPLATE_ID = "layer3-reasoning"
PROMPT_TEMPLATE_VERSION = "1.0.0"

DEFAULT_OUTPUT_CONTRACT = (
    '{"action_class": string | null, "confidence": number, "rationale": string}'
)


@dataclass(frozen=True)
class ActionClassDef:
    id: str
    description: str


@dataclass(frozen=True)
class PrecedentSummary:
    similarity: float
    freshness: str  # "fresh" | "stale"
    summary: str


def _load_template() -> str:
    return resources.files("shiftai_shared").joinpath("templates/layer3_user.md").read_text("utf-8")


def render_layer3_prompt(
    *,
    action_classes: list[ActionClassDef],
    case_data: dict[str, Any],
    output_contract: str = DEFAULT_OUTPUT_CONTRACT,
    closest_precedent: PrecedentSummary | None = None,
) -> str:
    """Fill the template. All untrusted free text must arrive inside ``case_data`` —
    it is serialized into the <case_data> block and nowhere else."""
    classes = "\n".join(f"- {c.id}: {c.description}" for c in action_classes)
    precedent = ""
    if closest_precedent is not None:
        precedent = (
            f"\nClosest precedent (similarity {closest_precedent.similarity:.2f}, "
            f"{closest_precedent.freshness}):\n{closest_precedent.summary}\n"
        )
    return _load_template().format(
        action_classes=classes,
        case_data_json=json.dumps(case_data, ensure_ascii=False, indent=2, default=str),
        precedent_section=precedent,
        output_contract=output_contract,
    )
