# Build & verification record — Agent 4 (Collaboration & Iteration)

Built 2026-09-03 per the approved `Agents/PLAN-collaboration-iteration.md`.

## Gates (all green at delivery)

| Gate | Result |
|---|---|
| `pytest` collaboration-iteration | 32 passed (review cycle, sweep ladder, marker shield, reconciliation, acceptance, static guardrails) |
| `pytest` bridge | 28 passed (incl. 4 Agent 4 lifecycle tests over HTTP; the old stand-ins now route through Agent 4) |
| `pytest` shared / A1 / A2 / A3 | 72(+4 gated) / 56 / 52 / 56 — no regressions (A3 +1: zero-marker escalation) |
| `mypy --strict` | clean ×6 packages |
| `ruff` | clean ×6 packages |
| `npm run build` (studio) | clean (tsc + vite) |
| Governance catalog | migration `0003_capability_c2c_agent4.sql` — all 5 new kinds registered; CI test enforces |

## Guardrails proven by test

- `content_confirmed` human-only: no package code path calls the gate (static);
  empty identity refused; confirmation with open conflicts **blocked**
- Conflicts: both positions quoted verbatim, held, never adjudicated; resolution
  carries the Marketing Lead's identity and feeds the next round as attributed feedback
- Marker shield: reworded/dropped marker sentences → section restored wholesale,
  edit flagged `sourced_claim_edit`; free sections still editable
- Never dropped: unparsable consolidation → everything deferred visibly; a model
  "losing" an item → reconciled back as deferred
- Version chain: gap in versions → `VersionCorruptionError`, asset halted, AiCoE routed
- Sweep ladder: business-day math (weekends skipped), monotonic reminders,
  escalation once with blocking reviewers + age
- Signal failure: confirmation stands, `tool_failure` escalated (retryable)

## Live verification (Azure dev, 2026-09-03, isolated bridge :8788)

Full loop on the real model: brief → approve → plan → confirms → **flagship
staged and auto-entered review** (reviewers + due date from the workflow plan) →
two reviewers left CONTRADICTORY feedback → the real consolidation round **held
both as one conflict** (`awaiting_conflict_resolution`), applied nothing →
**confirm while conflicted refused (409)** → Marketing Lead resolved with
identity → round 2 **applied the resolution as v2 with markers protected** →
human flagship confirm → fan-out (4 staged; `call_scripts` withheld on an
unparsable model output — degraded safely, **rework recovered it as v2**) →
derivatives auto-entered review → all confirmed through Agent 4's gate →
**manifest v1 (6 assets)**. Telemetry: 22 Agent 4 records, 7 human gates,
responding-model pricing (`gpt-5.4-nano`).

Observed by design: one live run produced a flagship with zero verified claim
markers (the model wrote marker-free prose; grounding stripped its one unsourced
claim to a gap note). Nothing false was staged, but the fan-out inherited an
empty claim inventory — now escalated visibly (`unsourced_claim`, tier 1) so a
human adds sourced proof points before confirmation. Expected to be rare on
production Opus.

## Known dev stand-ins (labeled in code/UI)

Feedback = studio review panel (Word comments bind via Graph later) · tracked
changes = new version + change log · sweep = invokable (scheduler at Execution
Studio onboarding) · signals = `BridgeSignals` (Execution Studio routes in prod)

## Outstanding (not blocking)

Reviewer-map sign-off (v0.1.0-draft, Marketing Lead) · Word/Excel Graph binding
(IT pending) · Approvals-queue mirror for conflict cards (conflicts currently
surface on the Content production draft cards — UI polish item) · Agent 5
(Quality Gate & Approval) consumes the manifest next
