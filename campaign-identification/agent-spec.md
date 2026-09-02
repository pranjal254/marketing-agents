# Agent Specification — Campaign Identification Agent

> Authored from `docs/agent-spec-template.md` (starter kit). Authoritative business
> content: LevelShift Content-to-Campaign Phase 1 Technical Specs V2.1, Agent 1 section
> + V2 Cross-Agent Standards. The JSON config (`config/campaign_identification.json`)
> is the authoritative Business Capability artifact; this document restates it.

## 0. Document control

| Field | Value |
|---|---|
| Spec version | 0.1.0 |
| Status | approved (user sign-off of PLAN.md, 2026-08-31) |
| Author | ShiftAI AiCoE (built via approved VS Code + Claude workflow) |
| Business owner | Marketing Lead / Manager (business rules & brief template) |
| Date | 2026-08-31 |

## 1. Identity & purpose

| Field | Value |
|---|---|
| Agent ID (`shiftai.agent.id`) | `campaign_identification` |
| Agent name | Campaign Identification Agent |
| Agent type | decision |
| Process / workflow | content-to-campaign (Phase 1, sub-process 1: Campaign Request Intake) |
| Risk tier | medium |
| Data classification | confidential (internal marketing plans & campaign strategy; no customer PII) |
| Trigger | event — new intake-form row (Forms→Excel workbook), new quarterly-plan row, flagged event-calendar entry, watched OneDrive folder; also on-demand from Execution Studio |
| SLA target | p95 < 60s request-submission → validated brief draft; 120s hard budget per run |

**Purpose.** Single front door for campaign demand: captures requests from the intake
form, quarterly marketing plan, and event calendar; normalizes each into a complete,
structured campaign brief; validates completeness, detects duplicates/conflicts against
in-flight campaigns, enforces BC/F&O independence, classifies (BU, vertical, Type 3/4
segment, channel mix); and routes the brief to the BU Campaign Lead. Humans stay in the
loop at two points: the requester answers targeted gap questions, and the BU Campaign
Lead gives the explicit approval without which no brief ever advances.

## 2. Reference architecture classification

- **Agent type rationale:** decision — it selects one action class per case
  (route/gaps/flag/escalate), holds a human gate, and its duplicate check plays the
  novelty-check role against the campaign calendar.
- **Memory needs:** campaign calendar + intake context in the Context Store
  (duplicate detection = precedent lookup with freshness decay, 90 days).
- **Reasoning provider:** claude (`claude-sonnet-5`); dev/test substitute:
  `azure_openai` behind the same `LLMProvider` interface; `mock` for unit tests.
  Selected by environment (`LLM_PROVIDER`), never hardcoded.
- **Restated invariant:** no Control Plane logic and no Execution Engine mechanics were
  added — those live in `shared/` (`shiftai_shared`), domain-free and reused by agents 2–5.

## 3. Business Capability Layer config

**Config file:** `config/campaign_identification.json` · **Version:** 0.1.0 · validated
by `shiftai_shared.business_capability.DecisionAgentConfig` (frozen at runtime).

### 3.1 Intake schema (mandatory brief-template fields)
objective, business_unit, vertical (financial_services | manufacturing | technology),
target_segment (type_3 | type_4 | standard), offer_topic, channels, timeline_start,
timeline_end, owner, budget_flag, requester — all required; free_text_context optional.

### 3.2 Policy rules (Layer 2, deterministic, first match wins in config order)
| Rule ID | Condition | Result action class |
|---|---|---|
| missing_mandatory_fields | any mandatory field missing/ambiguous | request_gaps |
| bc_fo_mixed | BC and F&O in one campaign concept | flag_bc_fo_mix |
| fresh_duplicate | open campaign, same BU+vertical, topic-similar, overlapping window, fresh record | flag_duplicate |
| authority-envelope.compliance-ceiling | pricing/legal/partner-commitment content | escalate tier 3 |

### 3.3 Action-class taxonomy
`route_for_approval` · `request_gaps` · `flag_bc_fo_mix` · `flag_duplicate` ·
`escalate_unclassifiable` (L3 may only choose from this list; anything else = abstention).

### 3.4 Authority envelope
| Field | Value |
|---|---|
| Impact ceiling | draft + route only; no approvals/rejections/merges; pricing/legal/partner always escalates |
| Reversibility | briefs versioned & additive; approval decision human-owned |
| Domain boundary | intake only; may not alter quarterly plan; **no Salesforce/Pardot access** (no connector exists in the codebase) |
| Max data age | 90 days (calendar-entry freshness decay) |
| Compliance ceiling | pricing / legal / partner commitments |

### 3.5 Routing map & tiers
data_ambiguity → requester (tier 1) · policy_gap → marketing-lead-queue (tier 2) ·
confidence_only → bu-campaign-lead-queue. Tier 3: compliance ceiling.

**STS reason-enum mapping (schema constraint):** `case_escalated.reason` enum lacks
`data_ambiguity`, so deterministic L2 escalations emit `policy_gap`, L3
abstention/low-confidence emits `low_confidence`, and the precise routing-map type
rides in additive `shiftai.escalation.uncertainty_type`.

### 3.6 Reason codes (versioned taxonomy, Standard C)
missing_field, ambiguous_field, unclassifiable_bu, duplicate_disputed, bc_fo_mix,
infeasible_timeline, requester_unresponsive, sla_breach, tool_failure.

### 3.7 Type-specific
Precedent (duplicate) decay window: 90 days. Confidence threshold for auto-routing:
0.6 (below → escalate). Max gap rounds before Marketing Lead escalation: 2.

## 4. Execution Engine binding

| Field | Value |
|---|---|
| Orchestration path | intake → L1 normalize → L2 policy (validation, BC/F&O, duplicates, compliance) → L3 reasoning (only if L2 clear) → envelope → kill switch → rate breaker → L4 (brief + routing) / escalate |
| Reasoning provider + model | claude / `claude-sonnet-5`, max_tokens 8000, prompt caching on both system blocks; dev = Azure OpenAI deployment |
| Prompt template | user message: `shiftai_shared` layer3 template @1.0.0 (injection guard intact); system: spec prompt verbatim @ `prompts/campaign-identification.system.v1.0.0.md` |
| Tools / actions (L4) | brief → Word doc (deterministic python-docx builder, no LLM) → workspace (OneDrive prod / local dev); approval task → Context Store (Execution Studio task routing at onboarding) |
| Idempotency store | `{case_id}:{action_class}:v{brief_version}` checked before upload/routing; SQLite (dev) — swap at onboarding |
| Memory | Context Store: campaign_calendar, case, intake_context, gap_request, approval_task, approved_brief, human_decision, failed_request |

### 4.1 Connections (per spec)
Receives: intake form / watched folder (new request), quarterly plan (reads), event
calendar (reads), BU Campaign Lead (approval/rejection/gap answers). Sends: Campaign-in-a-Box
Orchestrator (approved brief), Context Store (intake context).

### 4.2 Inputs
campaign_request (object), quarterly_plan (Excel rows), campaign_calendar (Context
Store), gap_answers (requester).

### 4.3 Outputs
campaign_brief (object + Word doc), intake_context (Context Store), gap_request
(requester).

## 5. Control Plane binding

**Target platform:** custom build (this kit) — standalone Python agent onboarded to
ShiftAI Execution Studio. **Recorded deviation:** kit build-spec §2 pins
Next.js/TypeScript/Prisma; the C2C Phase 1 spec + tech-stack brief mandate Python.
Ruling (project owner, 2026-08-31): Python implementation, kit architecture &
contracts binding, ported 1:1 (Zod→pydantic, Prisma→Context Store protocol).

| Component | Covered by | Notes |
|---|---|---|
| Kill switch / pause | `shiftai_shared.control_plane.KillSwitch` | checked immediately before every L4 action; scope: agent + tenant |
| Rate breaker | `RateBreaker` (window/max configurable) | trip engages kill switch before proceeding |
| Audit trail | STS v2 stream (telemetry IS audit) | append-only; no update/delete surface anywhere (tested) |
| Version registry | config/prompt/model versions on every record | `shiftai.config.version`, `prompt.template.*`, `model.version` |
| Drift monitor | Execution Studio dashboard (onboarding) | raw fields emitted; version-drift dashboard is platform-side |
| Injection guard | layer3 template `<case_data>` isolation | structural; acceptance test 3 |
| Backtest harness | deferred to Execution Studio onboarding | config changes replayed against stored cases (all inputs persisted) |
| Cost governor | `shiftai.cost.*` on every LLM span + run totals | rate-card model; budget alerting platform-side |
| RBAC | Execution Studio + M365 app registration (least privilege) | Graph scopes: Files.ReadWrite.Selected, Sites read as provisioned by IT |
| SLA monitor | alert conditions emitted (see §6.4); timers platform-side | |

## 6. Telemetry contract (STS v2.0.0)

Static values: tenant `levelshift-internal`, agent `campaign_identification` /
decision, config 0.1.0, risk medium, classification confidential, process
`content-to-campaign`. Every record validates against
`schemas/sts-core.schema.v2.0.0.json` at emit time (invalid = raise, never drop).

**Events:** case_intake, config_loaded, policy_check (L2), decision_made (L2/L3 — L3
carries gen_ai.*, prompt versions, span_incremental cost), tool_execution (workspace
upload), action_taken (idempotency key + external_ref + control states), case_escalated
(tier/reason/uncertainty + Context Package), human_gate (hitl.* + `shiftai.learn.*`:
reason_code, agent_recommendation, human_action, label, scenario_hash,
occurrence_count_90d, calibration_id, decision_latency_ms), case_resolved,
run_summary (run_total cost + llm/api/queue latency breakdown), error.

**Standard B extras (additive):** `shiftai.run.id`, `shiftai.latency.{llm,api,queue}_ms`,
`shiftai.request.source`, `shiftai.intake.completeness_score`,
`shiftai.intake.duplicate_flags`, `shiftai.business_object.*`.
**Deferred:** `autonomy_promotion` event (Standard D) — not in the STS v2 enum; raw
fields for the Autonomy Score are all emitted; flagged to AiCoE for STS v2.1.

### 6.3 Operational metrics (spec)
brief_first_pass_completeness ≥70% (from completeness_score) · intake_cycle_time_p95
< 1 business hour · duplicate_detection_rate ≥90% confirmed (learn.* labels) ·
approval_turnaround (decision_latency_ms), SLA 2 business days.

### 6.4 Alerting (spec → emitted signals)
| # | Condition | Signal |
|---|---|---|
| 1 | awaiting_input > 3 business days | case status + awaiting_since (platform timer) |
| 2 | approval gate > 2 business days | approval_task.created_at (reminder → Marketing Lead) |
| 3 | intake failure rate > 5% / 24h | `error` events / case_intake ratio |
| 4 | plan/calendar source unreadable | `error` event, error.type=GraphError/tool_failure |

## 7. Error handling & resilience

| Field | Value |
|---|---|
| Retry policy | 3 retries, exponential backoff from 2s — Graph + Anthropic/Azure transient failures only (429/5xx/timeouts); 4xx permanent |
| Timeout | 60s per external call; 120s per processing run (budget-checked at step boundaries) |
| Escalation | unclassifiable BU/vertical, disputed duplicates, 2 unanswered gap rounds → Marketing Lead; pricing/legal/partner → tier 3 |
| Fallback | fail-closed: raw request + structured failure record persisted (never discarded); briefs advance only via explicit approval; LLM gap-drafting failure falls back to deterministic questions (control flow never gated on the LLM) |

## 8. Governance & responsibility

### 8.1 Guardrails (spec, all implemented structurally + tested)
1. Never invent brief fields — enforced in code (`_enforce_never_invent`) + gap requests.
2. No advance without explicit BU Campaign Lead approval (identity + timestamp) — only
   `record_human_decision` can release; no other code path exists.
3. BC/F&O never combined — deterministic detector; split proposed, human decides.
4. Propose/flag only — no delete/merge/reject capability exists in the codebase.
5. No Salesforce/Pardot — no connector, no endpoint, no import (static test).

### 8.2 Explainability
Every classification cites named source fields (field_rationale); duplicate flags cite
conflicting campaign_ids + similarity; every record carries config/prompt/model
versions; full journey reconstructs from `shiftai.trace.id`.

### 8.3 Ownership
| Field | Value |
|---|---|
| Owner | ShiftAI AiCoE (build/operate) · Marketing Lead/Manager (rules & template) |
| Autonomy | may validate, classify, flag, draft, route; may NOT approve, reject, or alter the quarterly plan |
| Data sensitivity | internal-confidential marketing plans; secrets via env vars only, never in prompts/telemetry/logs |

## 9. Acceptance tests

All 10 kit rows implemented in `tests/test_acceptance_criteria.py` (see
CHECKLIST-campaign-identification.md for the row-by-row mapping), plus agent-specific:

| # | Test | Assertion |
|---|---|---|
| A1 | Injection cannot bypass gate | embedded "approve this yourself" instruction: reaches model only inside `<case_data>`; case still ends awaiting_approval |
| A2 | Gate integrity | approving an escalated case without a routed brief raises; identityless decisions raise |
| A3 | Never-invent | model-returned values for fields absent from the request are dropped |
| A4 | Gap-round ceiling | round 3 escalates `requester_unresponsive` to Marketing Lead |
| A5 | BC/F&O | mixed request escalates with split proposal; zero uploads |

### 4.4 AI-first intake extension (Marketing Studio, added 2026-08-31, user-approved)

- **L1 extraction** (`extraction.py`): fills empty brief fields from the requester's
  own description only — quoted provenance per field (`derived_fields`); segment,
  budget, dates and owner are never extracted (product rule: they stay human).
- **hold_for_verification**: new `draft_review` status — the drafted brief stays with
  the requester; `release_brief` records their verification (human_gate) before the
  routing action fires. Default flow (Forms/plan/calendar) is unchanged.
- **Revision loop** (`revision.py` + `revise_brief`): a human directive makes the
  agent rewrite requester-provided fields (never fills empty ones); every round is an
  audited human_gate `modified` with the recommendation/action delta (Standard C).
- **BU return-with-note**: `record_human_decision("returned")` → back to
  `draft_review` with the note as pending feedback; non-terminal, fully audited.

## 10. Open items

- Brief Word template + reason-code taxonomy are `0.1.0-draft` — Marketing Lead review
  before production (owner: Marketing Lead — before wave 1).
- Execution Studio bindings at onboarding: Context Store adapter, approval task
  routing, SLA timers, drift/cost dashboards (owner: AiCoE).
- `autonomy_promotion` event type missing from STS v2 enum (owner: AiCoE — STS v2.1).
- Microsoft Forms responses read via the Forms-linked Excel workbook (no stable Graph
  API for Forms) — confirm the workbook/table IDs per intake form (owner: AiCoE).
- Dev runs on Python 3.13 (no 3.12 on the dev machine); `requires-python >=3.12`.
