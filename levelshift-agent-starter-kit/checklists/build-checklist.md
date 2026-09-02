# Build Checklist

Two lists: the **build order** for the custom-build path (Technical Build Spec §10), and the platform-agnostic **reference checklist** from the reference architecture. Work top to bottom; do not reorder.

## A. Custom-build order (one agent, this kit's stack)

- [ ] 1. Agent spec authored from `docs/agent-spec-template.md` — no unresolved placeholders
- [ ] 2. Business Capability config authored against the matching `schemas/*.template.json`, validated with Zod
- [ ] 3. Prisma schema + migrations (Build Spec §8)
- [ ] 4. Control Plane primitives + their unit tests (kill switch, rate breaker, append-only audit)
- [ ] 5. Business Capability schema + read-only loader + template configs wired
- [ ] 6. Execution Engine skeleton with a stub `ReasoningProvider` that always abstains — proves orchestration end-to-end before any model call exists
- [ ] 7. `ClaudeReasoningProvider` + `prompts/layer3-reasoning.hbs` (no agent-specific prompt text anywhere)
- [ ] 8. `ActionExecutor` + idempotency store
- [ ] 9. Escalation path: Context Package assembly + human handoff console
- [ ] 10. Resolution submission → precedent write-back (closes the learning loop)
- [ ] 11. STS v2 emission at every orchestration step; fixtures validate against `schemas/sts-core.schema.v2.0.0.json` in CI
- [ ] 12. Audit/analytics view
- [ ] 13. End-to-end test on the placeholder template config with fabricated field names — the whole pipeline proven before any real business logic exists
- [ ] 14. All acceptance tests in `checklists/acceptance-criteria.md` green

## B. Reference checklist (any platform)

| # | Item | Plane |
|---|---|---|
| 1 | Business Capability Layer config authored and versioned | Business Capability Layer |
| 2 | Platform Selection Rubric applied, target platform confirmed (once per engagement) | Cross-plane |
| 3 | Control Plane native-feature mapping completed for the target platform — gaps identified | Control Plane |
| 4 | Control Plane wrapper built for any unmet requirements | Control Plane |
| 5 | Execution Engine implemented or bound on the target platform | Execution Engine |
| 6 | Tool/action interface implemented via MCP where supported | Execution Engine |
| 7 | Reasoning interface bound per Model Selection Rubric outcome | Execution Engine |
| 8 | Audit trail verified end-to-end on the target platform | Control Plane |
| 9 | Kill switch tested on the target platform — actual pause behavior, not just configuration | Control Plane |
| 10 | Backtest run against historical cases before go-live | Control Plane |
| 11 | STS v2 telemetry verified: one real case reconstructs fully from `shiftai.trace.id` | Cross-plane |
