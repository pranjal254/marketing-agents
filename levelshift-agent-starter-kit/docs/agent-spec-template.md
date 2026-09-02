# Agent Specification — <<Agent Name>>

> **How to use this template.** Copy it to `agent-spec.md` in your project root and fill every `<<placeholder>>`. A field you cannot determine yet is written as **Not determined** — never filled with a plausible value. The spec is complete when §3 (the Business Capability config) matches a validated JSON config in `schemas/`, and no `<<placeholder>>` remains. **No implementation before that.**
>
> This template is organized around the three-plane reference architecture: everything domain-specific lives in §3, and only §3. If you find yourself writing domain logic in any other section, stop — that's a design violation.

---

## 0. Document control

| Field | Value |
|---|---|
| Spec version | 0.1.0 |
| Status | draft \| in-review \| approved |
| Author | <<name / team>> |
| Business owner | <<who owns the rules this agent enforces>> |
| Date | <<yyyy-mm-dd>> |

## 1. Identity & purpose

| Field | Value |
|---|---|
| Agent ID (`shiftai.agent.id`) | <<stable-kebab-or-snake-slug>> — must equal `agentId` in the config |
| Agent name | <<Human-readable name>> |
| Agent type | decision \| enrichment \| orchestrator |
| Process / workflow | <<e.g. lead-to-opportunity>> |
| Risk tier | low \| medium \| high \| critical |
| Data classification | public \| internal \| confidential \| restricted |
| Trigger | user \| schedule \| event \| agent \| api — <<describe the concrete trigger>> |
| SLA target | <<e.g. p95 < 60s intake-to-decision>> |

**Purpose (one paragraph).** <<What business problem this agent removes, what it may decide on its own, and where humans stay in the loop. Written for a reviewer who has never seen the process.>>

## 2. Reference architecture classification

- **Agent type rationale:** <<why decision / enrichment / orchestrator>>
- **Memory needs:** episodic precedent store (decision agents) \| case-state memory (orchestrators) \| none (enrichment)
- **Reasoning provider:** claude \| local \| rubric — selected per the Model Selection Rubric; recorded in the config, never hardcoded
- **Restated invariant:** this agent adds **no** Control Plane logic and **no** Execution Engine mechanics. Everything specific to it lives in §3.

## 3. Business Capability Layer config (the domain content)

> The authoritative artifact is the JSON config validated against the matching template in `schemas/`. This section restates it for human review. Keep the two in sync — the JSON wins.

**Config file:** `config/<<agent-id>>.json` · **Version:** <<0.1.0>>

### 3.1 Intake schema
| Field | Type | Required | Notes |
|---|---|---|---|
| <<field>> | text \| number \| date \| boolean \| select | yes/no | <<options / provenance>> |

### 3.2 Policy rules (Layer 2)
| Rule ID | Condition (JSON-Logic) | Result action class |
|---|---|---|
| <<rule_id>> | <<expression>> | <<action_class_id>> |

### 3.3 Action-class taxonomy
| ID | Label | Description |
|---|---|---|
| <<action_class_id>> | <<Label>> | <<what this action class means — the reasoning layer may only choose from this list>> |

### 3.4 Authority envelope
| Field | Value |
|---|---|
| Impact ceiling | <<per-tier rule, e.g. "may auto-approve up to X">> |
| Reversibility rules | <<condition → reversibility class>> |
| Domain boundary | <<what this agent is and isn't allowed to touch>> |
| Max data age | <<days — cases with staler data escalate>> |
| Compliance ceiling | <<optional — regulatory line that always escalates>> |

### 3.5 Routing map & escalation tiers
| Uncertainty type | Routes to |
|---|---|
| data_ambiguity | <<queue/role>> |
| policy_gap | <<queue/role>> |
| confidence_only | <<queue/role>> |

| Tier | Condition |
|---|---|
| 1 | <<condition>> |
| 2 | <<condition>> |
| 3 | <<condition>> |

### 3.6 Reason codes
`<<reason_code_1>>`, `<<reason_code_2>>`, …

### 3.7 Type-specific config
- **Decision:** precedent decay window: <<90>> days.
- **Enrichment:** source schema, enrichment rules, output schema, insufficient-data confidence threshold: <<0.5>>.
- **Orchestrator:** stages (id, handled-by agent, on-success / on-escalation transitions), control scope (which child agents this orchestrator's pause cascades to).

## 4. Execution Engine binding

| Field | Value |
|---|---|
| Orchestration path | intake → L1 classify → L2 policy → <<novelty check? decision agents only>> → L3 reasoning → envelope → kill switch → rate breaker → L4 action / escalate |
| Reasoning provider + model | <<e.g. claude / claude-sonnet-5 — or rubric: name the rubric>> |
| Prompt template | `prompts/layer3-reasoning.hbs` @ <<version>> — no agent-specific prompt text exists |
| Tools / actions (Layer 4) | <<each action class → target system, MCP server if available, else ActionExecutor implementation>> |
| Idempotency store | <<where executed keys are checked>> |
| Memory | <<episodic precedent store / case-state / none>> |

### 4.1 Connections
| Direction | Connected node | Condition / signal |
|---|---|---|
| Receives from | <<node>> | <<signal>> |
| Sends to | <<node>> | <<signal>> |

### 4.2 Inputs
| Name | Type | Description | Source |
|---|---|---|---|

### 4.3 Outputs
| Name | Type | Description | Destination |
|---|---|---|---|

## 5. Control Plane binding

**Target platform:** custom build (this kit) \| Agentforce \| Copilot Studio/Foundry \| LangGraph \| Bedrock AgentCore

| Control Plane component | Covered by | Notes |
|---|---|---|
| Kill switch / pause | <<native feature / kit implementation / wrapper>> | checked before every L4 action |
| Rate breaker | <<…>> | window: <<minutes>>, max auto-executions: <<n>> |
| Audit trail | <<…>> | append-only, record at every step |
| Version registry (config/prompt/model versions) | <<…>> | |
| Drift monitor | <<…>> | baseline + schedule |
| Injection guard | <<…>> | free-text intake is data, never instructions |
| Backtest harness | <<…>> | required before any config/model change goes live |
| Cost governor | <<…>> | budget: <<amount/period>> |
| RBAC | <<…>> | roles: <<who edits config, who reviews cases>> |
| SLA monitor | <<…>> | per-tier resolution SLAs |

Platform-native coverage vs LevelShift wrapper is recorded here per component — gaps stay visible, never assumed covered.

## 6. Telemetry contract (STS v2)

**Schema:** `schemas/sts-core.schema.v2.0.0.json`. Every orchestration step emits a record; the stream is the audit trail.

### 6.1 Static attribute values
| Attribute | Value |
|---|---|
| `shiftai.agent.id` | <<agent-id>> |
| `shiftai.agent.type` | <<type>> |
| `shiftai.tenant.id` | <<tenant>> |
| `shiftai.process.name` | <<process>> |
| `shiftai.risk.tier` / `shiftai.data.classification` | <<values>> |

### 6.2 Events this agent emits
List the §5-taxonomy events this agent produces and when. Minimum for a decision agent: `case_intake`, `config_loaded`, `decision_made`, `policy_check`, `action_taken` **or** `case_escalated`, `human_gate` (if escalated), `case_resolved`, `run_summary`.

### 6.3 Operational metrics
| Metric | Description | Target |
|---|---|---|
| <<metric>> | <<what it measures and why it matters>> | <<target>> |

### 6.4 Alerting
| # | Condition | Threshold | Window |
|---|---|---|---|

## 7. Error handling & resilience

| Field | Value |
|---|---|
| Retry policy | <<e.g. 3 retries, exponential backoff from 2s, transient failures only>> |
| Timeout | <<per run / per call>> |
| Escalation | <<who, on what>> |
| Fallback | <<fail-closed behavior — never a silent partial result>> |

## 8. Governance & responsibility

### 8.1 Guardrails
| # | Guardrail |
|---|---|
| 1 | <<hard rule the implementation must make structurally impossible to violate>> |

### 8.2 Explainability
- **Decision logic:** <<how every output traces to named inputs, rules, and precedents>>
- **Human review triggers:** <<conditions that always route to a human>>

### 8.3 Ownership
| Field | Value |
|---|---|
| Owner (build/operate) | <<team>> |
| Business rules authority | <<role>> |
| Autonomy level | May autonomously <<…>>; may not <<…>> |
| Data sensitivity | <<what data it touches and the handling rules>> |

## 9. Acceptance tests

All seven standard tests from `checklists/acceptance-criteria.md`, plus agent-specific tests:

| # | Test | Assertion |
|---|---|---|
| A1 | <<agent-specific>> | <<assertion>> |

## 10. Open items

Listed honestly rather than hidden behind a completed-looking page:

- <<open item — owner — needed by>>
