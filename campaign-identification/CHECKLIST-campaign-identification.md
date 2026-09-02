# CHECKLIST — Campaign Identification Agent (Agent 1)

Both starter-kit checklists, ticked with evidence. Deviations are recorded, not hidden.
Gates: pytest 92/92 (42 shared + 50 agent) · mypy strict clean (37 files) · ruff clean.

## A. Custom-build order (checklists/build-checklist.md §A)

- [x] 1. Agent spec authored from the template — `agent-spec.md`, no unresolved placeholders (open items listed honestly in §10).
- [x] 2. Business Capability config authored + validated — `config/campaign_identification.json`, validated by pydantic port of the kit schema (`shiftai_shared.business_capability`). *Deviation: Zod→pydantic per Python ruling (spec vs kit conflict C1, user-approved).*
- [x] 3. Persistence schema — Context Store protocol + SQLite/in-memory implementations (versioned, append-only). *Deviation: Prisma/Postgres→Context Store protocol; production binding at Execution Studio onboarding (C5).*
- [x] 4. Control Plane primitives + unit tests — kill switch, rate breaker (trip→pause), append-only audit (`shared/tests/test_control_plane.py`).
- [x] 5. Business Capability schema + read-only loader wired — no write surface (tested).
- [x] 6. Engine skeleton proven with stub provider — `MockLLMProvider`; abstention path proves orchestration end-to-end without any model (acceptance test 10).
- [x] 7. Reasoning provider + single layer3 template — `AnthropicClient` (+ `AzureOpenAIClient` dev substitute) behind `LLMProvider`; user-message mechanics live only in `shiftai_shared/templates/layer3_user.md` @1.0.0. *Deviation C2 (user-approved): the spec's exact per-agent system prompt is versioned business-capability content (`prompts/campaign-identification.system.v1.0.0.md`); the kit template's injection guard/abstention/JSON contract stay intact in the shared template (tested).*
- [x] 8. ActionExecutor + idempotency store — `execute_idempotent` gate before the L4 side effect; repeat returns prior result (acceptance test 4/4b).
- [x] 9. Escalation path — Context Package on every escalation (`shiftai.context_package`), tiers + routing per config; human handoff = Execution Studio task + CLI in dev. *Console UI deferred to Execution Studio (platform owns review UI).*
- [x] 10. Resolution → precedent write-back — approval registers the campaign in the calendar store, which future duplicate checks (the precedent lookup) read (tested: test_fresh_duplicate_escalates_for_human_decision).
- [x] 11. STS v2 emission at every step; kit fixtures validate in CI — emit-time schema validation (raise on invalid); `shared/tests/test_telemetry.py::test_kit_fixtures_all_validate`.
- [x] 12. Audit/analytics view — telemetry JSONL + full journey reconstructable from trace_id (tested); dashboards are Execution Studio-side (deferred, recorded §5/§6 of agent-spec).
- [x] 13. End-to-end test on placeholder-free pipeline — full lifecycle with fabricated request data, mocked connectors (`tests/test_orchestration_e2e.py`).
- [x] 14. All acceptance tests green — `tests/test_acceptance_criteria.py` (mapping below).

## B. Reference checklist (build-checklist.md §B)

| # | Item | Status |
|---|---|---|
| 1 | Business Capability config authored + versioned | ✅ 0.1.0 |
| 2 | Platform selection | ✅ custom build (Python) → Execution Studio onboarding; ruling recorded (C1) |
| 3 | Control Plane native-feature mapping | ✅ agent-spec §5 table; gaps visible (drift/cost dashboards, SLA timers, backtest = platform-side) |
| 4 | Control Plane wrapper built for unmet requirements | ✅ `shiftai_shared.control_plane` |
| 5 | Execution Engine implemented | ✅ `orchestration.py` state machine |
| 6 | Tool/action interface | ✅ internal executor + idempotency (no MCP server available for M365 in this environment — internal fallback per kit §5.3; noted for onboarding) |
| 7 | Reasoning interface bound per rubric | ✅ claude-sonnet-5 via provider interface; azure_openai dev substitute |
| 8 | Audit trail verified end-to-end | ✅ acceptance test 8 + 9 |
| 9 | Kill switch tested — actual pause behavior | ✅ acceptance test 1 |
| 10 | Backtest before go-live | ⚠ deferred to Execution Studio onboarding (all case inputs persisted for replay) — open item |
| 11 | One real case reconstructs from trace_id | ✅ e2e test asserts single trace across the full journey |

## Acceptance criteria (checklists/acceptance-criteria.md) → tests

| Row | Test |
|---|---|
| 1 Kill switch | `test_1_kill_switch_pauses_all_action` — 3 auto-resolvable cases, all escalated, zero action_taken |
| 2 Rate breaker | `test_2_rate_breaker_trips_and_engages_kill_switch` |
| 3 Injection guard | `test_3_injection_in_free_text_cannot_bypass_gate` (+ prompt-structure tests in shared + classify suites) |
| 4 Idempotency | `test_4_same_idempotency_key_one_side_effect`, `test_4b_reprocessing_same_case_version_uploads_once` |
| 5 Config versioning | `test_5_old_case_keeps_original_config_version` |
| 6 Precedent freshness | `test_6_stale_calendar_entry_never_blocks_alone` (duplicate-decay mapping, PLAN.md Q6, user-approved) |
| 7 Plane isolation | `shared/tests/test_plane_isolation.py` (domain-term + import scan) + `test_7_agent_owns_domain_shared_owns_none` |
| 8 Telemetry validity + order | `test_8_full_case_valid_records_in_order` (emit-time validation + canonical sequence) |
| 9 Audit append-only | `test_9_no_update_or_delete_surface_anywhere` (+ shared sink/store surface tests) |
| 10 Abstention | `test_10_abstention_escalates_nothing_executes` — null action class → Context Package escalation, zero executions |

## Spec "Implementation Tasks" 1–10 → code

| Task | Where |
|---|---|
| 1 three entry points → common record | `intake.py` (`normalize_request`) |
| 2 validate vs brief schema | `validation.py` (config-driven, missing-field codes) |
| 3 duplicates/conflicts | `conflicts.py` (+ calendar in Context Store) |
| 4 BC/F&O split-or-flag | `rules.py` (`check_bc_fo`) |
| 5 classify (type/priority/channels) | `classify.py` (Sonnet 5; deterministic priority suggestion) |
| 6 targeted gap requests + awaiting_input | `gaps.py` + `_handle_gaps` |
| 7 Word brief w/ provenance | `brief.py` (deterministic docx; template 0.1.0-draft) |
| 8 route for approval; explicit human gate | `approval.py` + `record_human_decision` |
| 9 intake summary → Context Store | `persistence.py` (`save_intake_context`) |
| 10 telemetry per request | `orchestration.py` (completeness score, gap rounds, duplicate flags, full envelope) |

**Explicitly deferred (with reason):** Execution Studio bindings (store adapter, task
routing, SLA timers, dashboards, backtest harness) — platform-side at onboarding;
`autonomy_promotion` event — blocked on STS v2 enum (AiCoE); production brief template
sign-off — Marketing Lead.
