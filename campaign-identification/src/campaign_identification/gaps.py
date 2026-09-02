"""Task 6 — targeted gap requests: specific questions per missing/ambiguous field,
never a generic bounce. The brief holds in awaiting_input.

The LLM (Sonnet 5 / Azure GPT in dev) phrases the questions; if it fails, a
deterministic per-field template takes over so a gap request always goes out —
the LLM improves wording, it never gates the control flow.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from shiftai_shared.business_capability import DecisionAgentConfig
from shiftai_shared.llm import LLMProvider, LLMResponse, SystemBlock

from campaign_identification import MODEL_ID
from campaign_identification.models import (
    CampaignRequest,
    GapQuestion,
    GapRequest,
    MissingField,
)

_FALLBACK_TEMPLATES = {
    "missing": "Please provide the '{field}' for this campaign request.",
    "ambiguous": "The value provided for '{field}' is ambiguous ({detail}); please clarify.",
}


def _fallback_questions(missing: list[MissingField]) -> list[GapQuestion]:
    return [
        GapQuestion(
            field=m.field,
            question=_FALLBACK_TEMPLATES[m.kind].format(field=m.field, detail=m.detail or ""),
        )
        for m in missing
    ]


def draft_gap_request(
    provider: LLMProvider,
    system: list[SystemBlock],
    request: CampaignRequest,
    missing: list[MissingField],
    *,
    round_number: int,
    case_id: str | None = None,
    timeout_s: float = 60.0,
) -> tuple[GapRequest, LLMResponse | None]:
    """Draft one targeted question per gap. Returns the GapRequest plus the LLM
    response when the model produced usable questions (None on fallback)."""
    questions: list[GapQuestion] = []
    response: LLMResponse | None = None
    user = (
        "Draft one specific, targeted question per missing or ambiguous brief field "
        "below so the requester can complete their campaign request. Do not ask about "
        "anything else and do not infer answers.\n\n"
        "<case_data>\n"
        + json.dumps(
            {
                "request": request.model_dump(),
                "gaps": [m.model_dump() for m in missing],
            },
            indent=2,
            default=str,
        )
        + "\n</case_data>\n\n"
        'Respond with ONLY valid JSON: {"questions": [{"field": string, "question": string}]}'
    )
    try:
        response = provider.complete(
            system=system,
            user=user,
            model=MODEL_ID,
            max_tokens=2000,
            temperature=0.0,
            timeout_s=timeout_s,
        )
        payload = json.loads(response.text.strip().removeprefix("```json").removesuffix("```"))
        wanted = {m.field for m in missing}
        questions = [
            GapQuestion(field=q["field"], question=q["question"])
            for q in payload.get("questions", [])
            if q.get("field") in wanted and q.get("question")
        ]
    except Exception:
        questions = []
        response = None
    covered = {q.field for q in questions}
    for m in missing:
        if m.field not in covered:
            questions.extend(_fallback_questions([m]))
    return (
        GapRequest(
            case_id=case_id or request.request_id,
            round=round_number,
            questions=questions,
            sent_to=request.requester or "requester",
            created_at=datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        ),
        response,
    )


def escalation_needed(round_number: int, max_rounds: int) -> bool:
    """Two unanswered gap requests escalate to the Marketing Lead (spec escalation)."""
    return round_number > max_rounds


def gap_reason_codes(missing: list[MissingField], config: DecisionAgentConfig) -> list[str]:
    codes = []
    for m in missing:
        code = "missing_field" if m.kind == "missing" else "ambiguous_field"
        if code in config.reason_codes:
            codes.append(code)
    return sorted(set(codes))
