# PLAN — Agent 4: Content Collaboration & Iteration Agent

**Status: APPROVED 2026-09-03 ("lets do it") — BUILT. See
`collaboration-iteration/agent-spec.md` and `CHECKLIST-collaboration-iteration.md`
for what shipped and the verification record. OneDrive/SharePoint binds behind
the existing seams when IT grants API access.**
Spec: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 4 (authoritative; lines 690–872).
Practices: identical to approved Agents 1–3 (starter-kit binding, shared package, STS v2 telemetry, spec prompt verbatim/versioned, mock-everything tests, human gates never automated, guardrails in code, mypy strict/ruff clean, Azure GPT in dev).

## 0. Approved scope decisions (user, 2026-09-03)

| Decision | Choice |
|---|---|
| Feedback source | Word comments need Graph (IT pending). **Studio review panel** in dev: reviewers comment per asset/section in Content production; stored via the bridge behind a `FeedbackSource` protocol — the Word-comments connector binds later, zero agent change. |
| Stale sweep | **Built as an invokable `sweep()`** (bridge endpoint + CLI): stale detection vs due dates, graduated reminders, escalation to the Marketing Lead. Scheduler binding is an Execution Studio onboarding line. |
| Edit application | **New version + change log** per round: revised docx + per-round edit summary (every feedback item applied / deferred / conflicted / flagged), claim-marker protection enforced in code. Word tracked changes arrive with Graph. |
| UI wiring | **Same build — the dev stand-ins retire.** Flagship confirm and "Mark content-confirmed" become real Agent 4 review flows; confirmations auto-signal fan-out (flagship) and packaging registration (derivatives). |

## 1. Package layout (mirrors agents 1–3)

```
Agents/collaboration-iteration/
├── config/collaboration_iteration.json   # reviewer-role map, reminder ladder, max-rounds
│                                         #   alert (3), routingMap, reasonCodes (v0.1.0-draft)
├── prompts/collaboration-iteration.system.v1.0.0.md   # spec system prompt VERBATIM
├── src/c2c_collaboration/
│   ├── models.py          # FeedbackItem, NormalizedItem (location/instruction/reviewer/type),
│   │                      #   ConflictRecord (both positions QUOTED, held section), ReviewRound,
│   │                      #   EditSummary, IterationMetrics; LLM contracts:
│   │                      #   ConsolidationLLMOutput, RevisionLLMOutput
│   ├── feedback.py        # FeedbackSource protocol; dev binding reads store-backed comments
│   │                      #   (studio panel via bridge); Word-comments connector later
│   ├── assignments.py     # step 1: reviewers from the workflow plan (editorial → Content
│   │                      #   Writer, message fit → Marketing Lead), due dates from plan
│   │                      #   entries, review tasks + notifications recorded in the store
│   ├── consolidation.py   # steps 2-4: L3 Sonnet normalize/de-dupe/classify. Enforced in code:
│   │                      #   EVERY input item appears exactly once in the output (never
│   │                      #   silently dropped — spec Fallback); contradictions → ConflictRecord
│   │                      #   with both positions quoted, section HELD, never adjudicated
│   ├── revision.py        # step 5: L3 applies textual edits section-wise to Agent 3's stored
│   │                      #   draft. Marker protection in CODE: every [c-N] marker must survive;
│   │                      #   a marker-bearing sentence changed → edit flagged + original
│   │                      #   restored, routed sourced_claim_edit — never applied
│   ├── versions.py        # step 7: version chain integrity (parent version + sha256 per round);
│   │                      #   chain corruption → halt asset, escalate AiCoE (spec Alerting)
│   ├── documents.py       # revised docx (new version, never overwrite) + edit-summary section;
│   │                      #   status-tracker CSV update (Excel binding at Graph onboarding)
│   ├── sweep.py           # step 9: stale detection vs due dates; reminder ladder
│   │                      #   (due → reminder, +1bd → second, +2bd → escalate w/ blocking
│   │                      #   reviewer + age)
│   ├── metrics.py         # step 10: rounds, feedback volume, time-in-review per asset →
│   │                      #   iteration_metrics records (sub-process 5's raw material)
│   ├── orchestration.py   # CollaborationAgent: on_draft_staged(), run_review_round()
│   │                      #   [collect → consolidate → classify → apply/flag → summary],
│   │                      #   resolve_conflict() [Marketing Lead identity], confirm_content()
│   │                      #   [HUMAN-ONLY gate], sweep(); signals on confirm: flagship →
│   │                      #   Agent 3 fan-out, derivative → Agent 2 packaging registration
│   └── cli.py
└── tests/                 # unit + acceptance per spec steps 1-10 + static guardrails
```

New store kinds (+ catalog migration `0003_capability_c2c_agent4.sql` — the
governance CI test forces this): `review_assignment`, `feedback_item`,
`review_round`, `conflict_record`, `iteration_metrics`. Shared: no rewrites.

## 2. Model & providers

Prod `claude-sonnet-5`, adaptive thinking effort medium, 16k max tokens; dev
Azure GPT behind the provider seam; 10-minute budget per revision run; retries
3× exp from 2s. Prompt caching on system prompt + brand block (Standard A).
Event-driven invocation stays event-driven: bridge endpoints fire it (staged
draft → assignment; feedback-complete → round; confirm → signals).

## 3. Guardrails enforced in code (spec §Governance, all tested)

1. `content_confirmed` is human-only: the state transition exists ONLY inside
   `confirm_content(actor_id, actor_role)`; no agent code path calls it (static
   test, like Agent 3's). A confirmation record without human identity is
   impossible by construction (spec alert target: zero).
2. Conflicts are never adjudicated: contradictory items are excluded from the
   revision call by code, held with both positions quoted, resolved only via
   `resolve_conflict()` carrying the Marketing Lead's identity.
3. Claim-marker protection is deterministic: markers must survive revision
   verbatim; marker-sentence changes are reverted + flagged, never applied.
4. Additive versions only: revised drafts are new versions through the additive
   workspace protocol (overwrites structurally impossible); version chain hashes
   verified each round.
5. No item silently dropped: consolidation output is reconciled against input —
   every feedback item ends applied / deferred / conflicted / rejected-by-human.

## 4. Telemetry & escalation (STS v2, existing emitter)

- Per-round `decision_made` (consolidation, revision) with prompt template
  versions; `human_gate` for confirmations + conflict resolutions (identity,
  latency); `case_escalated` w/ reason codes `feedback_conflict`,
  `sourced_claim_edit`, `stalled_asset`, `max_rounds_exceeded` (>3 — signals an
  outline/brief problem), `version_corruption` (halt → AiCoE), `tool_failure`
- Metrics per spec: `iteration_rounds_avg`, `time_in_review_p95`,
  `feedback_application_accuracy` (re-correction tracking), `stale_asset_count`
- Reviewer comments are internal-only (spec Data Sensitivity): quoted inside
  workspace records and conflict cards, never into telemetry attributes.

## 5. Bridge + Marketing Studio (same build — stand-ins retire)

- Endpoints: `POST .../assets/{aid}/feedback` (reviewer comment w/ identity),
  `POST .../assets/{aid}/feedback-complete` (triggers the round),
  `GET .../assets/{aid}/review` (rounds, summaries, conflicts, state),
  `POST .../conflicts/{cid}/resolve` (Marketing Lead), `POST .../assets/{aid}/confirm`
  (**the real human gate — replaces both dev stand-ins**; flagship confirm
  auto-signals Agent 3 fan-out; derivative confirm registers REAL bytes with
  Agent 2 packaging), `POST /api/box/campaigns/{id}/sweep`
- Studio Content production: per-asset review thread (comment box for
  writers/leads with BusyButton states), round history + edit summaries
  ("what changed" at a glance), conflict cards routed to the Marketing Lead in
  Approvals, confirm button now = Agent 4's gate; stale chips from sweep results
- Review tasks appear in Approvals queues per assignee (existing task mirror)

## 6. Test plan

- Unit: consolidation reconciliation (drop-proof), conflict hold, marker
  protection (delete/reword cases), version-chain corruption halt, sweep ladder
  timing (business days), metrics math
- Acceptance: one test per spec implementation step 1–10
- Static: agent never calls `confirm_content` on itself; no publish/send
  surface; plane isolation extended; kinds catalog (0003) covered by the
  existing governance test automatically
- Gates: pytest all packages, mypy --strict, ruff, bridge TestClient, npm build;
  live Azure end-to-end (draft → feedback → round → conflict → resolve →
  confirm → fan-out/packaging) before done

## 7. Order of work

1. Models + config + verbatim prompt + migration 0003 (catalog rows)
2. feedback (protocol + store binding) → assignments → consolidation (tests)
3. revision + marker protection → versions → documents → metrics (tests)
4. sweep → orchestration + signals + CLI + acceptance tests
5. Bridge endpoints (retire stand-ins) + studio review UI → browser verify on Azure dev
6. Docs: agent-spec.md, CHECKLIST, README; deploy notes unchanged

## Open items (not blocking)

Word comments + Excel tracker via Graph (IT pending — protocol seams ready) ·
scheduler binding for the sweep (Execution Studio onboarding) · reviewer-role
map sign-off (v0.1.0-draft, Marketing Lead) · Agent 5 consumes the confirmed
package (next build)
