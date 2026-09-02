"""Task 5 — Layer 3 classification with Claude Sonnet 5 (dev: Azure GPT behind the
same provider interface).

System prompt: the exact spec text, loaded from prompts/ (versioned, cached block).
User message: the kit's single Layer 3 template — all request free text goes inside
<case_data> (injection guard). Output is strict JSON; parse failure retries once,
then abstains (escalation, never a guess).

Guardrail 1 enforcement is deterministic and lives here: normalized_fields coming
back from the model may only echo/normalize values already present on the request —
anything else is dropped, so the model structurally cannot invent brief fields.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from shiftai_shared.business_capability import DecisionAgentConfig
from shiftai_shared.llm import LLMProvider, LLMResponse, SystemBlock
from shiftai_shared.prompting import ActionClassDef, render_layer3_prompt

from campaign_identification import MAX_OUTPUT_TOKENS, MODEL_ID, SYSTEM_PROMPT_VERSION
from campaign_identification.models import (
    BcFoCheck,
    CampaignRequest,
    ClassifyOutput,
    ConflictFlag,
    ValidationResult,
)

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / f"campaign-identification.system.v{SYSTEM_PROMPT_VERSION}.md"

OUTPUT_CONTRACT = (
    '{"action_class": string | null, "confidence": number, "rationale": string,'
    ' "classification": {"campaign_type": "demand_gen" | "offering_launch_support"'
    ' | "event_follow_up", "priority": "high" | "medium" | "low",'
    ' "channel_mix": string[], "segment_relevance": string,'
    ' "field_rationale": {<brief_field>: <named source>}},'
    ' "normalized_fields": {<brief_field>: string}}'
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

# Fields the model may normalize; a value is kept only when the request already
# carries a value for that field (never-invent rule, enforced in code).
_NORMALIZABLE_FIELDS = {
    "objective",
    "business_unit",
    "vertical",
    "target_segment",
    "offer_topic",
    "owner",
    "timeline_start",
    "timeline_end",
}


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip()


def system_blocks(config: DecisionAgentConfig) -> list[SystemBlock]:
    """Cached blocks: the versioned spec system prompt + the stable schema/taxonomy
    context (both stable across requests — prompt-caching targets)."""
    stable_context = json.dumps(
        {
            "brief_template_schema": [f.model_dump() for f in config.intake_schema],
            "action_class_taxonomy": [a.model_dump() for a in config.action_class_taxonomy],
            "reason_codes": config.reason_codes,
        },
        indent=2,
    )
    reference = (
        f"Reference data (brief template schema, action classes, reason codes):\n{stable_context}"
    )
    return [
        SystemBlock(text=load_system_prompt(), cache=True),
        SystemBlock(text=reference, cache=True),
    ]


def build_case_data(
    request: CampaignRequest,
    validation: ValidationResult,
    conflicts: list[ConflictFlag],
    bc_fo: BcFoCheck,
    suggested_priority: str,
) -> dict[str, Any]:
    return {
        "campaign_request": request.model_dump(),
        "validation": validation.model_dump(),
        "conflicts": [c.model_dump() for c in conflicts],
        "bc_fo_check": bc_fo.model_dump(),
        "suggested_priority": suggested_priority,
    }


def derive_priority(request: CampaignRequest, plan_linked: bool) -> str:
    """Deterministic priority suggestion: vertical priority + BU plan linkage
    (spec Task 5). The model may override with cited rationale."""
    vertical_rank = {"financial_services": 2, "manufacturing": 1, "technology": 2}
    score = vertical_rank.get(request.vertical or "", 0) + (1 if plan_linked else 0)
    if score >= 3:
        return "high"
    if score == 2:
        return "medium"
    return "low"


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", cleaned).strip()
    match = _JSON_BLOCK.search(cleaned)
    if match is None:
        raise ValueError("no JSON object in model output")
    data: dict[str, Any] = json.loads(match.group(0))
    return data


def _enforce_never_invent(output: ClassifyOutput, request: CampaignRequest) -> ClassifyOutput:
    kept: dict[str, str] = {}
    for field, value in output.normalized_fields.items():
        if field not in _NORMALIZABLE_FIELDS:
            continue
        if getattr(request, field, None) in (None, "", []):
            continue  # request had no value: a gap request, never an inferred value
        kept[field] = value
    return output.model_copy(update={"normalized_fields": kept})


def run_classification(
    provider: LLMProvider,
    config: DecisionAgentConfig,
    case_data: dict[str, Any],
    request: CampaignRequest,
    *,
    timeout_s: float = 60.0,
) -> tuple[ClassifyOutput, LLMResponse, str]:
    """One L3 pass. Returns (validated output, raw provider response, user prompt).

    Abstention semantics: parse/validation failure after one retry returns an
    explicit abstention (action_class=None, confidence 0.0) — escalate, never guess.
    """
    action_classes = [ActionClassDef(a.id, a.description) for a in config.action_class_taxonomy]
    user_prompt = render_layer3_prompt(
        action_classes=action_classes,
        case_data=case_data,
        output_contract=OUTPUT_CONTRACT,
    )
    blocks = system_blocks(config)
    response = provider.complete(
        system=blocks,
        user=user_prompt,
        model=MODEL_ID,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0.0,
        timeout_s=timeout_s,
    )
    for attempt in (1, 2):
        try:
            parsed = ClassifyOutput.model_validate(_extract_json(response.text))
            if (
                parsed.action_class is not None
                and parsed.action_class not in config.action_class_ids()
            ):
                raise ValueError(f"action_class {parsed.action_class!r} not in taxonomy")
            return _enforce_never_invent(parsed, request), response, user_prompt
        except (ValueError, ValidationError, json.JSONDecodeError):
            if attempt == 2:
                break
            response = provider.complete(
                system=blocks,
                user=user_prompt + "\n\nYour previous reply was not valid JSON per the contract. "
                "Respond with ONLY the JSON object.",
                model=MODEL_ID,
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.0,
                timeout_s=timeout_s,
            )
    abstention = ClassifyOutput(
        action_class=None,
        confidence=0.0,
        rationale="model output unparsable after retry — abstaining",
    )
    return abstention, response, user_prompt
