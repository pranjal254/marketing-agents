"""Task 2 — validate the request against the brief template schema (deterministic).

Driven by the Business Capability config's intakeSchema; enumerates every missing or
ambiguous field with a code. Emits the completeness score used by telemetry (Task 10).
"""

from __future__ import annotations

from shiftai_shared.business_capability import IntakeField

from campaign_identification.models import CampaignRequest, MissingField, ValidationResult


def _value_for(request: CampaignRequest, field: str) -> object:
    return getattr(request, field, None)


def validate_request(
    request: CampaignRequest, intake_schema: list[IntakeField]
) -> ValidationResult:
    missing: list[MissingField] = []
    required = [f for f in intake_schema if f.required]
    for field_def in required:
        value = _value_for(request, field_def.field)
        if value is None or value == "" or value == []:
            missing.append(
                MissingField(
                    field=field_def.field,
                    code=f"missing_{field_def.field}",
                    kind="missing",
                    detail="mandatory brief-template field not provided",
                )
            )
            continue
        if field_def.type == "select" and field_def.options and str(value) not in field_def.options:
            missing.append(
                MissingField(
                    field=field_def.field,
                    code=f"ambiguous_{field_def.field}",
                    kind="ambiguous",
                    detail=f"value {value!r} not in {field_def.options}",
                )
            )
    resolved = len(required) - len(missing)
    score = resolved / len(required) if required else 1.0
    return ValidationResult(
        missing=missing,
        complete=not missing,
        completeness_score=round(score, 4),
    )
