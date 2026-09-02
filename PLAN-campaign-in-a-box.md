# PLAN — Agent 2: Campaign-in-a-Box Orchestrator

**Status: APPROVED 2026-09-02 ("lets move forward") — BUILT. See
`campaign-in-a-box/agent-spec.md` and `CHECKLIST-campaign-in-a-box.md` for what
shipped and the verification record.**
Spec: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 2 (authoritative).
Practices: identical to the approved Agent 1 build (starter-kit architecture, shared package, STS v2 telemetry, mock-everything tests, Azure dev provider).

## 0. Approved scope decisions (user, 2026-09-02)

| Decision | Choice |
|---|---|
| SemRush | No key yet. Real connector built against SemRush API shape, unit-tested with mocks. Dev runs **intel-library-only fallback, flagged** (spec's documented fallback). `SEMRUSH_API_KEY` env placeholder; zero code change when key arrives. |
| Scope | **Both halves**: LLM planning pass (steps 1–8) + deterministic packaging module (steps 9–12). Packaging tested with synthetic content-confirmed assets (played by dev bridge until Agents 3–4 exist). |
| Composition | Defined as versioned config **v0.1.0-draft, pending Marketing Lead sign-off**, using only asset types the spec names. |
| UI testing | Bridge + Marketing Studio wired in this same build. |

## 1. Package layout (mirrors campaign-identification)

```
Agents/campaign-in-a-box/
├── config/campaign_in_a_box.json        # versioned business-capability config (below)
├── prompts/campaign-in-a-box.system.v1.0.0.md   # spec system prompt VERBATIM
├── src/c2c_campaign_box/
│   ├── models.py          # pydantic: PlanCase, IntelClaim, AudienceOfferPack, AssetChecklistItem,
│   │                      #   ContentOutline, WorkflowPlan, PackageManifest, CompletenessDiff …
│   ├── intake.py          # step 1: load approved campaign_brief from Context Store;
│   │                      #   reject non-approved with structured error
│   ├── intel.py           # step 2: SemRush + intel-library gathering; per-claim source URI +
│   │                      #   retrieved_at; unverified → flagged + excluded from proof points
│   ├── repository.py      # step 5: repository search by BU/vertical/topic/asset-type;
│   │                      #   deterministic fitness scoring (config weights)
│   ├── planning.py        # steps 3,4,6: L3 Opus planning pass → audience & offer pack,
│   │                      #   messaging angles, reuse/adapt/create rationale, outlines
│   ├── grounding.py       # never-invent enforcement: every proof point/claim in LLM output
│   │                      #   must cite a gathered source URI or brief field — else stripped
│   │                      #   to gaps[]; banned terms / BC-F&O / Copilot-cloud-only lint
│   ├── calendar.py        # step 7: deterministic back-planning (campaign window, review
│   │                      #   gates, 2-researched-blogs/month rule) → infeasibility report
│   ├── workspace.py       # step 7: campaign workspace from versioned folder/naming template;
│   │                      #   status tracker init (LocalWorkspace dev / OneDrive prod)
│   ├── confirmation.py    # step 8: Marketing Lead / BU Lead gate; delta application;
│   │                      #   infeasible-timeline escalation with trade-offs
│   ├── packaging/
│   │   ├── state.py       # asset/package state machine: planned → in_production →
│   │   │                  #   content_confirmed → packaged_pending_compliance; rework re-open
│   │   ├── completeness.py# diff vs checklist (missing/extra/version-mismatch); non-empty → block
│   │   ├── naming.py      # naming+metadata validation; auto-correct unambiguous only, flag rest
│   │   ├── snapshot.py    # copy finals → final-assets folder; sha256 per asset
│   │   └── manifest.py    # transactional assembly; partial failure → remove snapshots, revert
│   ├── orchestration.py   # plan_campaign(), confirm_pack(), confirm_plan(), mark_confirmed()
│   │                      #   [dev stand-in for Agents 3–4], run_packaging(), reopen_assets()
│   └── cli.py
└── tests/                 # all external mocked; acceptance tests from spec steps 1–12
```

Shared additions (`shiftai_shared`): `semrush/client.py` (retry/backoff per spec: 3×, exp from 2s, 60s timeout; quota-exhaustion → fallback signal), `hashing.py` (sha256 snapshot helper — Agent 5 reuses it). Nothing in shared is rewritten.

## 2. Config v0.1.0-draft (pending Marketing Lead sign-off)

- **Composition** (asset types — all named in spec; flagship + 8 derivatives ≈ the spec's "~9 assets"): `flagship_blog`, `email_touchpoints`, `linkedin_posts`, `faq_service_page`, `community_draft`, `external_one_pager`, `battle_card`, `call_scripts`, `enablement_notes`
- Capacity rule: 2 researched blogs/month · review gates: flagship 3 business days, derivative 2 (spec Agent 4 SLAs)
- Thin-intel alert threshold: >30% candidate claims unverified
- Confirmation-pending alert: >2 business days
- Fitness-score weights + reuse/adapt thresholds; naming template `<campaign-slug>-<asset-type>-vN`; folder template per `docs/marketing-folder-structure.md`
- Volume caps per derivative type (consumed later by Agent 3; registered here since checklist is the authority)

## 3. Model & providers

- Prod: `claude-opus-5`, adaptive thinking effort high, streaming, 16k max tokens; prompt caching on system prompt + rules/brand blocks (existing SystemBlock cache mechanism)
- Dev: existing provider layer → `azure_openai` (gpt-5.4-nano) / `mock`; no code path differences
- Packaging module: **no LLM anywhere** (pure functions; property of the module, asserted in tests)

## 4. Guardrails enforced in code (spec §Governance)

1. Unsourced claim ⇒ `unverified`, excluded from proof points (grounding.py strips; telemetry records)
2. Orchestrator never confirms its own outputs — pack/plan effective only after identity-stamped human confirmation (same gate pattern as Agent 1)
3. Repository is read-only: no write API is ever handed a repository path (static test, like plane isolation)
4. Packaging: only assets with a human confirmation record; non-empty diff blocks — never padded/trimmed; content untouched (names/metadata/copies/manifest only)
5. BC/F&O independence, Copilot-D365-cloud-only, banned terms, "ShiftAI" one word — deterministic lint on all pack/outline text; **no Salesforce/Pardot code exists** (static test); audience is segment/persona-level only

## 5. Telemetry & escalation (STS v2, existing emitter)

- Full envelope per event; `cost.scope` run_total/span_incremental; pack/checklist/plan/package versions; every SemRush query + intel read with URI/timestamp; reuse decisions w/ fitness scores; completeness diffs; hashes; confirmation identities
- Reason codes: `thin_intel`, `unsourced_claim`, `infeasible_timeline`, `completeness_block`, `tool_failure`; hash mismatch / snapshot failure → escalate AiCoE
- Alerts per spec: thin intel, infeasible window, missing confirmation record or hash mismatch (halt), SemRush fallback engaged, confirmation pending >2bd

## 6. Bridge + Marketing Studio

- Bridge endpoints: `POST /api/cases/{id}/plan` (trigger on approved case — mirrors spec's "on brief approval"), `GET /api/plans/{id}`, `POST /api/plans/{id}/confirm-pack`, `POST /api/plans/{id}/confirm-plan` (deltas), `POST /api/plans/{id}/assets/{aid}/confirm` (**dev-only stand-in** for Agents 3–4, clearly labeled), `POST /api/plans/{id}/package`, reopen endpoint, doc downloads
- Studio: Live console shows the planning pass + telemetry; approved-brief mirror flow gains "Plan campaign" step; pack/plan confirmation gate surfaces like the Approvals gate did for Agent 1

## 7. Dev bindings (same seam as Agent 1)

- LocalWorkspace for campaign workspace/snapshots; local seeded folders for `02-Reference/intel-library` and `03-Repository` (sample assets with metadata for reuse search); SQLite context store; live Graph/SemRush swap-in later via env, no code change

## 8. Test plan

- Unit: every module; SemRush client (mock transport, quota/timeout/fallback paths); grounding strips unsourced; calendar infeasibility cases; packaging transactionality (inject failure mid-snapshot → no partial manifest, state reverted); naming auto-correct vs flag; state machine transitions incl. rework re-open + re-hash
- Acceptance: one test per spec implementation step 1–12
- Static: repository read-only, no-Salesforce/Pardot, packaging-has-no-LLM
- Gates: pytest green, mypy --strict clean, ruff clean; bridge TestClient tests; npm build clean

## 9. Order of work

1. Shared: semrush client + hashing → tests
2. Models + config + prompt file
3. intake → intel → repository → planning+grounding → calendar → workspace → confirmation (each with tests)
4. Packaging module (pure) + state machine → tests
5. Orchestration + CLI + acceptance tests
6. Bridge endpoints + studio wiring → browser-verify on Azure dev
7. Docs: agent-spec.md, CHECKLIST, README updates

## Open items (not blocking)

- SemRush key (env placeholder ships now) · composition sign-off by Marketing Lead · M365 creds (IT request pending) · Azure key rotation (user)
