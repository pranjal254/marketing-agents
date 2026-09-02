# ShiftAI Telemetry Standard — STS v2.0.0

**Schema:** `schemas/sts-core.schema.v2.0.0.json`
**Fixtures:** `telemetry/fixtures.json`
**Status:** v2 is a from-scratch redesign. It replaces STS v1.x entirely.

---

## 1. What changed in v2, and why

STS v2 is rebuilt around the CTO reference architecture. Telemetry is no longer a separate onboarding program — it **is** the Control Plane's event stream, made durable and queryable.

**Removed from v1.x (deliberately, not deprecated-but-tolerated):**

- **The central agent registry and `shiftai.agent.registry_id`.** There is no registration step and no registry ID to request. An agent's identity is the `agentId` declared in its own Business Capability Layer config, emitted as `shiftai.agent.id`. Identity lives in the versioned config, where the architecture already keeps it.
- **Conformance tiers (Bronze / Silver / Gold) and certification runs.** There is one flat contract: a required core, conditional rules, and optional fields. A record is either valid against the schema or it is not.
- **Onboarding classes (A/B/C) and `shiftai.telemetry.class` / adapter fields.** Agents built from this kit emit natively. A platform-bound agent maps its native telemetry to this schema as part of its Platform Binding (spec template §5); how the record was produced is not part of the record's contract.
- **The completeness score.** With no Silver tier there is nothing to score. Validity is binary.

**Reframed:** every STS v2 record is an **audit record**. The CTO build spec requires an append-only audit entry at every orchestration step, carrying the layer and the config/model/prompt versions active at the time. STS v2 records carry exactly those fields, so the telemetry stream and the audit trail are the same discipline.

## 2. Alignment with the Control Plane event contract

The Control Plane consumes five generic events from the Execution Engine. STS v2 encodes them 1:1 and adds the operational envelope around them:

| Control Plane event (CTO spec §4) | STS v2 event type | Notes |
|---|---|---|
| `decision_made` | `decision_made` | Carries `shiftai.decision.*` (action class, confidence, layer 1/2/3) |
| `action_taken` | `action_taken` | Carries `shiftai.action.*` (class, idempotency key, external ref) |
| `case_escalated` | `case_escalated` | Carries `shiftai.escalation.*` (tier 1/2/3, reason, routed-to) |
| `config_loaded` | `config_loaded` | `shiftai.config.version` is required on every record anyway |
| `latency_ms` | — | Not a separate event: every record MAY carry `shiftai.span.duration_ms` |

Operational envelope events: `case_intake`, `case_resolved`, `tool_execution`, `policy_check`, `human_gate`, `error`, `run_summary`.

## 3. Namespaces

| Namespace | What lives there |
|---|---|
| `shiftai.*` | LevelShift governance: identity, case lifecycle, decisions, actions, escalations, control-plane checks, HITL, cost, risk |
| `gen_ai.*` | OTel GenAI semantic conventions, used verbatim: model, tokens, tool calls, conversation. Present only on LLM- or tool-bearing events |
| `deployment.*`, `error.*` | Standard OTel resource / error attributes |
| Vendor namespaces (`salesforce.*`, `anthropic.*`, …) | Passthrough, preserved verbatim, never validated |

Rules that carry over from v1 unchanged: extend OTel, never fork it; never fabricate a missing value (emit it absent, not guessed); prompt/completion **content is never collected** — telemetry carries versions, hashes, and references, not text.

## 4. Required on every record (the core 12)

| Attribute | Type | Meaning |
|---|---|---|
| `shiftai.schema.version` | string (semver) | `2.0.0` |
| `shiftai.tenant.id` | string | Client organisation ID — basis of isolation and RBAC |
| `shiftai.agent.id` | string | The `agentId` from the agent's Business Capability config. Self-declared, stable, no registry |
| `shiftai.agent.type` | enum | `decision` \| `enrichment` \| `orchestrator` |
| `shiftai.config.version` | string | Business Capability config version active for this case — makes every record explainable after later config changes |
| `deployment.environment.name` | enum | `production` \| `staging` \| `dev` |
| `shiftai.timestamp` | ISO-8601 UTC | Event time |
| `shiftai.event.type` | enum | See §5 |
| `shiftai.case.id` | string | The case this record belongs to. Cases are the unit of work in the reference architecture |
| `shiftai.trace.id` | string | Correlation ID across agents/stages of one process journey |
| `shiftai.risk.tier` | enum | `low` \| `medium` \| `high` \| `critical` — from the agent spec |
| `shiftai.data.classification` | enum | `public` \| `internal` \| `confidential` \| `restricted` — highest touched |

## 5. Event taxonomy

| `shiftai.event.type` | Emitted when | Required with it |
|---|---|---|
| `case_intake` | A case enters the agent | — |
| `config_loaded` | Config resolved for the case (orchestration step 1) | — |
| `decision_made` | L1/L2/L3 produced a decision (or abstention) | `shiftai.decision.action_class` (nullable = abstention), `shiftai.decision.confidence`, `shiftai.decision.layer`, `shiftai.layer` |
| `policy_check` | Authority envelope / policy rules evaluated | `shiftai.policy.decision` |
| `action_taken` | A Layer 4 action fired | `shiftai.action.class`, `shiftai.action.idempotency_key` |
| `case_escalated` | Case routed to a human tier | `shiftai.escalation.tier`, `shiftai.escalation.reason` |
| `human_gate` | A human decision was recorded | `shiftai.hitl.decision`, `shiftai.hitl.actor.role` |
| `case_resolved` | Terminal resolution (agent or human) | `shiftai.outcome`, `shiftai.resolution.outcome_source` |
| `tool_execution` | A tool/MCP call completed | `gen_ai.tool.name`, `shiftai.span.duration_ms` |
| `error` | A handled or unhandled error | `error.type` |
| `run_summary` | End-of-run rollup | `shiftai.outcome` |

## 6. Audit fields (per CTO AuditRecord)

| Attribute | Type | Meaning |
|---|---|---|
| `shiftai.layer` | enum | `L1` \| `L2` \| `novelty` \| `L3` \| `L4` \| `escalation` \| `resolution` — which orchestration step produced this record |
| `shiftai.config.version` | string | Required core (see §4) |
| `shiftai.model.version` | string | Model that served this step; absent if the step didn't call a model |
| `shiftai.prompt.template.id` / `.version` | string | Which version of `prompts/layer3-reasoning.hbs` was filled. Required on any record carrying `gen_ai.request.model` |

## 7. Domain-neutral payload fields

Decision (`shiftai.decision.*`): `action_class` (string or null — null is explicit abstention), `confidence` (0–1), `layer` (1 | 2 | 3).

Action (`shiftai.action.*`): `class`, `idempotency_key`, `external_ref` (ID of the record touched in the target system).

Escalation (`shiftai.escalation.*`): `tier` (1 | 2 | 3), `reason` (`novelty` | `low_confidence` | `policy_gap`), `routed_to` (queue/role, never a personal identity).

Control-plane checks (`shiftai.control.*`): `kill_switch` (`clear` | `paused`), `rate_breaker` (`ok` | `tripped`).

Precedent / learning (`shiftai.precedent.*`, on novelty-check and resolution records): `case_id`, `similarity` (0–1), `freshness` (`fresh` | `stale`); `shiftai.resolution.outcome_source` (`agent` | `human` | `seed`) closes the learning loop on `case_resolved`.

HITL (`shiftai.hitl.*`): `decision` (`approved` | `rejected` | `modified` | `timeout`), `actor.role` (role, never personal identity).

Policy (`shiftai.policy.*`): `ids` (string[]), `decision` (`allow` | `deny` | `escalate` | `redact`).

Outcome: `shiftai.outcome` (`success` | `failure` | `partial` | `escalated` | `cancelled` | `unknown`) on terminal events.

Spans: `shiftai.span.id`, `shiftai.parent.span.id`, `shiftai.span.duration_ms` — this is how the Control Plane's `latency_ms` event is realized.

Cost (`shiftai.cost.*`): `amount`, `currency` (ISO-4217), `model` (`measured` | `rate_card` | `estimated`), `scope` (`run_total` | `span_incremental` — prevents double-counted rollups).

GenAI (`gen_ai.*`, verbatim OTel): `request.model`, `response.model`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read.input_tokens`, `usage.reasoning.output_tokens`, `tool.name`, `tool.call.id`, `conversation.id`, `response.finish_reasons`.

Process context (optional): `shiftai.process.name`, `shiftai.stage.id` (orchestrator agents), `shiftai.business_object.type` / `.id` (generalized business item a case is about).

## 8. Conditional rules (enforced by the schema)

1. `decision_made` → `shiftai.decision.action_class` (may be null), `shiftai.decision.confidence`, `shiftai.decision.layer`, `shiftai.layer` all present.
2. `action_taken` → `shiftai.action.class` and `shiftai.action.idempotency_key` present.
3. `case_escalated` → `shiftai.escalation.tier` and `shiftai.escalation.reason` present.
4. `human_gate` → `shiftai.hitl.decision` and `shiftai.hitl.actor.role` present.
5. `error` event or `shiftai.outcome = failure` → `error.type` present.
6. `case_resolved` → `shiftai.outcome` and `shiftai.resolution.outcome_source` present.
7. `gen_ai.request.model` present → `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `shiftai.prompt.template.version` present.
8. `shiftai.cost.amount` present → `shiftai.cost.currency`, `shiftai.cost.model`, and `shiftai.cost.scope` present.
9. `tool_execution` → `gen_ai.tool.name` and `shiftai.span.duration_ms` present.
10. `policy_check` → `shiftai.policy.decision` present.
11. `run_summary` → `shiftai.outcome` present.

## 9. What good looks like

One case flowing through a decision agent produces, at minimum: `case_intake` → `config_loaded` → `decision_made` (with layer) → `policy_check` → either `action_taken` or `case_escalated` → (`human_gate` if escalated) → `case_resolved` → `run_summary`. Every record shares the `shiftai.case.id`; the whole journey reconstructs from `shiftai.trace.id` alone. See `telemetry/fixtures.json` for a complete worked sequence.
