# PLAN.md — Content to Campaign Phase 1 · Agent 1 (Campaign Identification)

**Status:** approved 2026-08-31 (user: "i trust your understanding move forward with the plan" — recommendations in §5/§6 accepted). Phase 1 build complete; see `campaign-identification/CHECKLIST-campaign-identification.md`.
**Sources read end-to-end:** spec V2.1 HTML (Agent 1 section + V2 Cross-Agent Standards A–D), C2C-Phase1-Tech-Stack.html, and the full starter kit (CLAUDE.md, reference-architecture, technical-build-spec, telemetry-standard, sts-core.schema.v2.0.0.json, agent-spec-template, both checklists, fixtures, prompt template, config templates).

---

## 1. Repo layout (proposed)

```
Agents/
├── PLAN.md
├── shared/                              # reusable across agents 1–5 (pip-installable)
│   ├── pyproject.toml                   # dist: shiftai-shared · package: shiftai_shared
│   ├── src/shiftai_shared/
│   │   ├── config.py                    # env-only settings (pydantic-settings); no secrets in code
│   │   ├── llm/
│   │   │   ├── provider.py              # LLMProvider protocol (provider-agnostic, per kit rule)
│   │   │   ├── anthropic_client.py      # Claude via Anthropic SDK; prompt caching on system+stable blocks
│   │   │   └── azure_openai_client.py   # dev/test provider (Azure GPT), same interface
│   │   ├── m365/
│   │   │   ├── graph_client.py          # MS Graph auth (client-credential), timeout/retry/backoff
│   │   │   ├── forms.py                 # intake form responses (via Excel-backed responses file)
│   │   │   ├── excel.py                 # workbook/table reads (quarterly plan, tracker)
│   │   │   ├── onedrive.py              # drive items, watched-folder listing, upload, locking
│   │   │   └── word.py                  # .docx generation from template (deterministic, no LLM)
│   │   ├── telemetry/
│   │   │   ├── emitter.py               # STS v2.0.0 emitter; validates each record against the kit schema
│   │   │   ├── schema.py                # loads sts-core.schema.v2.0.0.json (single source of truth)
│   │   │   └── envelope.py              # trace/run/span ids, cost calc, latency breakdown, learn.* fields
│   │   ├── context_store/
│   │   │   ├── store.py                 # CampaignContextStore protocol
│   │   │   └── local_store.py           # SQLite/JSON impl for dev; Execution Studio binding later
│   │   ├── resilience.py                # retry w/ exponential backoff (3× from 2s), timeouts, idempotency store
│   │   └── control_plane.py             # kill switch, rate breaker, append-only audit (thin, domain-free)
│   └── tests/
├── campaign-identification/             # Agent 1 (rename decision — see Open Q1)
│   ├── pyproject.toml
│   ├── agent-spec.md                    # filled from kit docs/agent-spec-template.md (kit hard rule 1)
│   ├── config/campaign_identification.json   # Business Capability config (versioned, read-only at runtime)
│   ├── prompts/
│   │   └── campaign-identification.system.v1.0.0.md   # EXACT system prompt from spec, verbatim, versioned
│   ├── src/campaign_identification/
│   │   ├── models.py                    # pydantic: CampaignRequest, CampaignBrief, GapRequest, IntakeContext…
│   │   ├── intake.py                    # Task 1 — normalize 3 entry points → common request record
│   │   ├── validation.py                # Task 2 — deterministic schema/completeness check (no LLM)
│   │   ├── conflicts.py                 # Task 3 — duplicate/timing conflict detection vs calendar + store
│   │   ├── rules.py                     # Task 4 — BC/F&O split-or-flag rule (deterministic)
│   │   ├── classify.py                  # Task 5 — Sonnet 5 classification (type, priority, channel mix)
│   │   ├── gaps.py                      # Task 6 — targeted gap request drafting; awaiting_input hold
│   │   ├── brief.py                     # Task 7 — Word brief assembly (deterministic packaging of LLM output)
│   │   ├── approval.py                  # Task 8 — route to BU Campaign Lead; human gate; never auto-approve
│   │   ├── persistence.py               # Task 9 — intake_context → Context Store
│   │   ├── orchestration.py             # case state machine wiring tasks 1–10 + telemetry (Task 10)
│   │   └── cli.py                       # local runner (dev): process one request end-to-end with mocks/live dev
│   ├── tests/                           # unit tests (mocked connectors) + telemetry fixture tests
│   ├── CHECKLIST-campaign-identification.md
│   └── README.md
└── levelshift-agent-starter-kit/        # untouched — extended, never rewritten
```

## 2. Shared components to extract (built this session, reused by agents 2–5)

- **LLM layer** — `LLMProvider` protocol (kit: "reasoning interface must never assume a specific provider"). `AnthropicClient`: Sonnet 5/Opus 5 routing by caller, prompt caching (`cache_control` on system prompt + stable context blocks), token/cost capture for telemetry. `AzureOpenAIClient`: dev/test substitute (you have Azure GPT in dev, no Claude) — same interface, maps usage fields; selected via `LLM_PROVIDER` env. Unit tests always mock; optional live smoke test targets Azure dev.
- **M365 connectors** — one Graph client (MSAL client-credential, env-var secrets), timeout + 3-retry exponential backoff from 2s (spec Error Handling), idempotent writes (idempotency key checked before side effects). Forms/Excel/OneDrive/Word wrappers expose only what Agent 1 needs; agents 2–5 extend later.
- **STS telemetry emitter** — every emitted record validated against `schemas/sts-core.schema.v2.0.0.json` at emit time; envelope adds Standard B extras (run_id, latency breakdown llm/api/queue, state transitions, business_object, cost with `cost.scope`) and Standard C `shiftai.learn.*` fields on `human_gate` — all legal additive attributes (`additionalProperties: true`).
- **Control-plane primitives** — kill switch check before every side-effecting action, rate breaker, append-only audit writer (audit = the STS stream, per kit), zero domain strings (kit plane-isolation rule; enforced by a static test).
- **Context Store client** — protocol + local (SQLite) impl for dev/tests; Execution Studio/production binding is an onboarding-time adapter.
- **Config & resilience** — pydantic-settings from env only; shared retry/timeout/idempotency helpers.

## 3. Agent 1 task breakdown (mapped to spec sections)

| Spec item | Implementation | LLM? |
|---|---|---|
| Task 1 ingest 3 entry points | `intake.py` + Forms/Excel/OneDrive connectors → `CampaignRequest` | No |
| Task 2 validate vs brief template schema | `validation.py`, pydantic brief schema, missing-field codes | No (deterministic) |
| Task 3 duplicate/conflict detection | `conflicts.py` vs campaign calendar + Context Store; cites conflicting campaign_ids | No (rules) + LLM assist only for topic-overlap similarity |
| Task 4 BC/F&O split-or-flag | `rules.py` deterministic; never silently merged | No |
| Task 5 classification (type, priority, channel mix) | `classify.py` → Sonnet 5, reasoning tied to named source fields; events channel for Type 3/4 | Yes |
| Task 6 targeted gap requests, `awaiting_input` hold | `gaps.py` → Sonnet 5 drafts specific questions; state held, never forwarded | Yes |
| Task 7 Word brief in workspace, provenance per field | `brief.py` — deterministic .docx assembly from structured LLM output + template | Drafting yes; packaging no |
| Task 8 route to BU Campaign Lead, explicit approval gate | `approval.py` — human_gate preserved exactly; brief advances only on recorded human approval (identity + timestamp) | No |
| Task 9 intake summary → Context Store | `persistence.py` | No |
| Task 10 telemetry per request | `orchestration.py` + shared emitter: completeness score, gap-request count, duplicate flags | No |
| Connections / Inputs / Outputs | pydantic models: campaign_request, quarterly_plan, campaign_calendar, gap_answers → campaign_brief, intake_context, gap_request | — |
| Telemetry & Monitoring metrics | brief_first_pass_completeness, intake_cycle_time_p95, duplicate_detection_rate, approval_turnaround emitted as run attributes | — |
| Logging | V2 envelope: trace/run/span, tokens/cost, llm/api/queue latency, versions; request_id/campaign_id/source; validation, flags, classification, approval events | — |
| Alerting | emitted as `error`/threshold events + local alert evaluator (Execution Studio wires real alerting at onboarding) | — |
| Error handling | 3 retries backoff-from-2s; 120s run timeout; escalation reasons per spec; fallback: persist raw request + structured failure — never discard; only `approved` briefs advance | — |
| Guardrails 1–5 | never-invent-fields (gap request instead), approval-required, BC/F&O, propose-don't-delete, no Salesforce/Pardot (no connector exists in code) | — |
| System Prompt | spec text verbatim → `prompts/campaign-identification.system.v1.0.0.md`, loaded at runtime, cached block | — |
| Standards A | Sonnet 5 only; prompt caching; deterministic steps LLM-free | — |
| Standards B/C/D | envelope fields + learn.* on human overrides; autonomy raw fields emitted (formula = dashboard, not agent) | — |

**Case flow (kit state machine mapped to Agent 1):** intake → L1 normalize/source-classify → L2 deterministic policy (validation, BC/F&O, duplicates) → L3 Sonnet 5 (classification, gap drafting, brief drafting) → envelope + kill-switch + rate-breaker → L4 actions (write Word brief, route approval task) or escalate (gap request / unclassifiable) → human_gate (BU Lead) → case_resolved + run_summary. STS events exactly per kit §9 sequence; fixtures.json is the reference.

## 4. Testing strategy

- Unit tests: every module, all external I/O mocked (Graph, Anthropic, Azure). No live calls, no real credentials.
- Telemetry fixture tests: our emitted sequences validate against `sts-core.schema.v2.0.0.json`; kit `telemetry/fixtures.json` also validated in CI as a regression anchor.
- Acceptance tests: kit `checklists/acceptance-criteria.md` rows implemented as pytest tests (kill switch, rate breaker, injection guard, idempotency, config versioning, plane isolation static test, telemetry validity, append-only audit, abstention path; precedent-freshness — see Q6).
- Gates: pytest + mypy (strict) + ruff all green; both checklists ticked in `CHECKLIST-campaign-identification.md`.
- Dev LLM: `LLM_PROVIDER=azure_openai` lets you smoke-test the real pipeline against Azure GPT in dev; prod config is `anthropic` + `claude-sonnet-5`.

## 5. Conflict list — spec vs starter kit (need your ruling)

| # | Conflict | Spec / project says | Starter kit says | Recommendation |
|---|---|---|---|---|
| C1 | **Language/stack** | Standalone **Python** agents (Tech-Stack doc, your brief: Py 3.12, pydantic, mypy, ruff) | CLAUDE.md rule 2 + build-spec §2 pin **Next.js/TypeScript/Prisma/Postgres/Zod/npm**; "do not substitute without updating the build spec document" | Build in Python; keep the kit's *architecture* (three planes, interfaces, STS, checklists) as binding, port TS interfaces 1:1 to Python protocols/pydantic. Record the deviation in agent-spec.md §5 + CHECKLIST. |
| C2 | **Prompt rule** | Spec provides an exact per-agent System Prompt; your brief: "use the exact prompts from the spec" | CLAUDE.md rule 5: *only* `prompts/layer3-reasoning.hbs`, "never write a new reasoning prompt string for a specific agent" | Use the spec prompt verbatim as the agent's versioned system prompt (it is the business-capability content); keep the layer3 template's non-negotiable mechanics (injection-guard `<case_data>` wording, JSON-only output, abstention) embedded in the runtime user-message template. |
| C3 | **STS version** | Spec V2.1 header + Standard B say "STS **v1.1**" (state.*, sla.*, learn.*, run_id, latency breakdown) | Kit: STS **v2.0.0** "replaces v1.x entirely"; schema requires `shiftai.schema.version` matching `2.x.x` | Emit STS v2.0.0 (kit schema is non-negotiable); carry all Standard B/C v1.1-era fields as additive attributes — schema allows `additionalProperties`. No data loss. |
| C4 | **`autonomy_promotion` event** (Standard D) | New STS event type | Not in the v2 event enum → would fail schema validation | Defer: promotion events are emitted by the learning system (sub-process 5), not Agent 1; Agent 1 already emits all required raw fields. Flag to AiCoE for an STS v2.1 schema addition. |
| C5 | **Persistence** | Campaign Context Store (unspecified DB) | Prisma + Postgres schema §8 | Context Store behind a Python protocol; SQLite locally, real binding chosen at Execution Studio onboarding. Kit's DB tables map to store collections (cases, decision logs, audit, context packages). |
| C6 | **Dev LLM = Azure GPT** (your note) | Claude only (both docs) | Claude behind swappable provider interface | The kit itself mandates provider-swappability — Azure client is dev-only, selected by env; prod stays claude-sonnet-5. No spec change needed; recorded as env policy. |

## 6. Open questions / assumptions

- **Q1 · Folder rename:** existing folder is `campaign-identification` (hyphen, no space). Options: (a) keep it, inner package `src/campaign_identification/` (valid Python either way — my recommendation), or (b) rename folder to `campaign_identification`. Your call.
- **Q2 · Agent type for STS:** enum is decision|enrichment|orchestrator. Agent 1 validates/classifies/flags and holds a human gate → I'll declare `decision` (with the duplicate-check playing the novelty role). Confirm.
- **Q3 · Intake form transport:** Microsoft Forms has no stable Graph API for responses; standard pattern is the Forms→Excel responses workbook (and/or watched OneDrive folder, which the spec names as the trigger). Plan: poll/receive the Excel responses table + watched folder; no invented Forms endpoint. Confirm.
- **Q4 · Approval capture in dev:** production approval arrives via Execution Studio task routing. For this session, approval/rejection/gap-answers enter through a CLI/store event (recorded with identity+timestamp, human_gate emitted). The gate itself is never bypassed. Confirm.
- **Q5 · Python version:** machine has 3.13 and 3.11, no 3.12. I'll target 3.12 semantics (`requires-python = ">=3.12"`) and run locally on 3.13 — or you install 3.12. Preference?
- **Q6 · Precedent/novelty acceptance test:** kit row 6 (precedent decay) assumes an episodic precedent store. For Agent 1 I map it to duplicate-detection freshness (open campaigns window). If you'd rather I implement a true precedent store now, say so; otherwise implemented as duplicate-freshness + documented mapping.
- **Q7 · Brief template & rules pack files:** spec references "the standard brief template" (Word) and versioned taxonomy codes. None ship with the kit. I'll create a versioned starter template + reason-code taxonomy marked `0.1.0-draft` for Marketing Lead review — no invented business content beyond the fields the spec enumerates. Confirm.
- **A1 · Assumption:** telemetry `shiftai.tenant.id = "levelshift-internal"`, `deployment.environment.name = "dev"` locally; risk tier `medium`, data classification `confidential` (spec: internal-confidential plans). Say if different.
- **A2 · Assumption:** no Salesforce/Pardot code anywhere (guardrail 5) — enforced by a static test like the plane-isolation one.
- **A3 · Assumption:** `shared/` is a sibling package under `Agents/`, versioned `0.1.0`, installed editable into the agent env.

## 7. Build order (Phase 1, after your OK)

1. `shared/` skeleton: config → resilience → telemetry emitter + schema validation (fixtures test green first) → control-plane primitives → LLM providers (mock-first) → M365 connectors (mock-first) → context store.
2. `agent-spec.md` filled from the kit template (no unresolved placeholders) + Business Capability config JSON.
3. Agent 1 modules in spec task order (1→10), orchestration last; prompts file verbatim from spec.
4. Tests continuously; then acceptance-criteria suite; mypy/ruff clean.
5. CHECKLIST ticked, README, demo run (mocked end-to-end + optional Azure dev smoke).

---
**Approve, or edit the recommendations in §5/§6, and I start Phase 1.**
