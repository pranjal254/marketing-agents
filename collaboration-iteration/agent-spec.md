# Agent Spec — Content Collaboration & Iteration Agent (Agent 4)

Filled from `levelshift-agent-starter-kit/docs/agent-spec-template.md`.
Authoritative source: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 4 + V2 Cross-Agent Standards A–D.

## Identity

| | |
|---|---|
| Agent ID | `collaboration_iteration` |
| Agent type (STS enum) | `decision` (AI Agent) |
| Model (production) | `claude-sonnet-5`, adaptive thinking effort medium, 16k max tokens |
| Model (dev/test) | Azure OpenAI behind the shared `LLMProvider` seam; mocks in unit tests |
| Timeout | 10 min per revision-application run (spec) |
| Risk tier / data class | medium / confidential — reviewer commentary is INTERNAL-ONLY, never quoted outside the workspace or into telemetry attributes |
| System prompt | `prompts/collaboration-iteration.system.v1.0.0.md` — spec verbatim, versioned |
| Config | `config/collaboration_iteration.json` v0.1.0 (reviewer map **0.1.0-draft**, pending Marketing Lead sign-off) |

## What it does (the review cycle, steps 1–10)

1. **Stage** (`on_draft_staged`): reviewers assigned from the workflow plan
   (flagship → Content Writer + Marketing Lead; derivative → Content Writer),
   due dates from the plan's review entries; refreshes on regenerated versions.
2. **Collect + consolidate** (`run_review_round`): reviewer comments (dev:
   studio panel via the bridge behind a `FeedbackSource` seam; prod: Word
   comments via Graph) → L3 normalize/de-duplicate/classify. **Reconciled in
   code**: every input item ends exactly once as applied / deferred / conflicted /
   routed-structural / logged-backlog / flagged / rejected-by-human.
3. **De-conflict**: contradictions become `conflict_record`s with BOTH positions
   quoted verbatim; the sections are held; the agent NEVER picks a side —
   `resolve_conflict` carries the Marketing Lead's identity, and the decision
   becomes attributed feedback for the next round.
4. **Classify + route**: structural items merge into ONE consolidated rework
   instruction signalled to Agent 3; out-of-scope ideas are logged for the
   sub-process-5 backlog without acting.
5. **Apply** textual edits as a NEW version (additive workspace — overwrites
   impossible) with a per-round edit summary. **Marker shield in code**: every
   `[c-N]` marker and its carrying sentence must survive verbatim; a violated
   section is restored wholesale and the edit flagged `sourced_claim_edit`.
6. **Confirm** (`confirm_content`): HUMAN-only, identity-stamped; open feedback
   is explicitly set aside (rejected-by-human) at confirmation; open conflicts
   BLOCK confirmation. Signals: flagship → fan-out unlock; derivative →
   packaging registration (with the real staged bytes + lineage via the bridge
   binding). `reopen_review` handles the gate_findings/re-open path.
7. **Sweep** (`sweep`): stale detection vs due dates in business days; ladder
   first reminder (at due) → second (+1bd) → escalation (+2bd, with blocking
   reviewer roles + age). Invokable now; the 4-business-hour scheduler binds at
   Execution Studio onboarding.
8. **Metrics** (`iteration_metrics`): rounds, feedback volume, conflicts,
   time-in-review, reminders — the raw material for sub-process 5.

## Signals seam (spec Connections, decoupled)

`Signals` protocol: `flagship_confirmed`, `register_confirmed`, `route_rework`.
Dev binding: `c2c_bridge/signals.py` (calls the co-hosted agents 2–3 directly).
Production: Execution Studio event routing. The agent never imports its
neighbors' orchestrators. Signal failures escalate `tool_failure` — the human
decision stands; signals are retryable.

## Guardrails (all enforced in code, tested)

1. `content_confirmed` is human-only — the transition exists only inside
   `confirm_content(actor_id, …)`; no package code path invokes it (static test);
   an empty actor identity is refused.
2. Conflicts surfaced, never adjudicated — conflicted items are excluded from
   the revision call by code.
3. Claim→source markers immutable without a human — the shield restores and flags.
4. Additive versions only + version-chain integrity check (contiguous 1..N);
   corruption halts the asset and pages AiCoE (`version_corruption`).
5. No feedback item silently dropped — output reconciled against input; an
   unparsable consolidation defers EVERYTHING visibly (`unclassified_feedback`).

## Store kinds (governance catalog: migration 0003)

`review_assignment` (per-asset state machine) · `feedback_item` ·
`review_round` · `conflict_record` · `iteration_metrics`. All flagged for
personal data where reviewer identity/commentary appears.

## Telemetry (STS v2, schema-validated at emit)

`decision_made` per L3 call (templates `collaboration-consolidation` /
`collaboration-revision` v1.0.0; responding-model pricing), `human_gate` for
confirmations/resolutions/re-opens with identity + decision latency,
`case_escalated` with reason codes (`feedback_conflict`, `sourced_claim_edit`,
`stalled_asset`, `max_rounds_exceeded` >3, `unclassified_feedback`,
`version_corruption`, `tool_failure`), `action_taken` for assignments /
revisions / reminders. Spec metrics: rounds avg, time-in-review, application
accuracy (flag events), stale count (sweep records).

## Dev bindings / production swap

| Concern | Dev | Production |
|---|---|---|
| Feedback source | studio review panel via bridge | Word comments via Graph (`FeedbackSource` seam) |
| Tracked changes | new version + change log in docx | Word tracked changes via Graph |
| Status tracker | CSV in workspace (Agent 2 pattern) | Excel via Graph |
| Sweep schedule | invokable endpoint/CLI | Execution Studio schedule (4 business hours) |
| Signals | `BridgeSignals` (co-hosted agents) | Execution Studio routing |
