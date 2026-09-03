# PLAN — Agent 3: Content Repurposing Agent

**Status: APPROVED 2026-09-03 ("lets build this agent") — BUILT. See
`content-repurposing/agent-spec.md` and `CHECKLIST-content-repurposing.md` for
what shipped and the verification record.**
Spec: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 3 (authoritative; lines 508–688).
Practices: identical to approved Agents 1–2 (starter-kit architecture, shared package, STS v2 telemetry, spec system prompt verbatim/versioned, mock-everything tests, never-invent enforced in code, human gates never automated, secrets env-only, mypy strict/ruff clean, Azure GPT in dev).

## 0. Approved scope decisions (user, 2026-09-03)

| Decision | Choice |
|---|---|
| Database layer | **Deferred.** OneDrive/SharePoint = system of record for content files; DB = metadata catalog (asset registry, hashes, driveItem IDs, lineage, HITL records). The `CampaignContextStore` protocol already isolates this — a `PostgresContextStore` adapter is added when OneDrive access lands or at the Azure prod phase, whichever first. Zero agent code change. Render free-tier ephemeral disk accepted for testing. |
| Flagship confirm gate | Agent 4 (Collaboration & Iteration) isn't built. **Studio stand-in, clearly labeled DEV**: Content Writer reviews the flagship in the studio and confirms; that human action triggers fan-out. Same precedent as Agent 2's asset-confirm stand-in. The gate is always a human. |
| Fan-out execution | **Provider seam now, Batch API at prod.** Dev: bounded-parallel per-derivative calls (Azure GPT). Prod Anthropic path binds to the Message Batches API (spec cost model, 50% off) behind the same seam. Azure OpenAI Batch (~24h) rejected — can't meet the 20-min fan-out SLA. |
| UI wiring | Same build. Placeholder checklist assets in the Content production tab become real drafts (flagship + derivatives, gap notes, self-check results, per-model telemetry). |

OneDrive/SharePoint access still pending with IT → Agent 3 writes to the LocalWorkspace mirror exactly like Agent 2; Graph swap is isolated behind the workspace seam.

## 1. Package layout (mirrors agents 1–2)

```
Agents/content-repurposing/
├── config/content_repurposing.json      # channel recipes, volume caps, retry/timeout, self-check policy (v0.1.0-draft)
├── prompts/content-repurposing.system.v1.0.0.md   # spec system prompt VERBATIM
├── src/content_repurposing/
│   ├── models.py          # FlagshipDraft, ClaimMarker, ClaimInventory(+items w/ ids), Derivative,
│   │                      #   GapNote, SelfCheckReport, ReworkRequest, FanoutRun …
│   ├── intake.py          # step 1: load content_outlines + audience_offer_pack + intel from Context
│   │                      #   Store (Agent 2 KIND_* records); validate every planned claim maps to a
│   │                      #   sourced proof point; unverifiable sections → gap notes, never drafted
│   ├── flagship.py        # steps 2–3: Opus L3 long-form draft (32k, streaming) grounded on proof
│   │                      #   points only; inline claim→source markers
│   ├── markers.py         # marker format `[c-N]` in text + sidecar claim-map JSON (claim id →
│   │                      #   source URI, quote). Upgrade path: real Word comments when Graph lands
│   ├── staging.py         # steps 4, 9: versioned .docx into campaign workspace (Agent 2 naming
│   │                      #   templates); register in Context Store w/ lineage; notify (bridge event).
│   │                      #   Additive & versioned — never overwrite reviewed content
│   ├── inventory.py       # step 5: parse confirmed flagship → claim inventory; every inventory item
│   │                      #   must quote flagship text verbatim (deterministic substring verify — LLM
│   │                      #   extraction, code-enforced grounding)
│   ├── fanout.py          # steps 6–7: derivatives per channel recipe from config; ONLY types on the
│   │                      #   approved asset checklist; volume caps from config; `generate_many`
│   │                      #   provider seam (dev parallel calls / prod Anthropic Batch)
│   ├── selfcheck.py       # step 8: shiftai_shared.brand lint + rules pack per-rule pass/fail codes;
│   │                      #   fail → regenerate (max 2), then withhold + gap note. Never stage a
│   │                      #   failed asset; stage the passing subset
│   ├── rework.py          # step 10: regenerate only the affected asset/section from rework_request
│   │                      #   (Iteration/Quality Gate codes); version bump, lineage update
│   ├── orchestration.py   # draft_flagship(), confirm_flagship() [identity-stamped human record],
│   │                      #   run_fanout(), apply_rework(); state machine enforces flagship-first
│   ├── cli.py
└── tests/                 # all external mocked; acceptance per spec steps 1–10
```

Shared: no rewrites. Reuses `brand` lint, `hashing`, workspace/naming seams, provider layer, STS emitter, fleet pricing (per-model cost already correct).

## 2. Never-invent enforcement (code, not prompt)

1. Flagship: every factual claim/statistic/competitor/ROI statement must carry a marker resolving to a gathered proof-point URI — unmarked claims in flagged categories → stripped to gap notes (grounding pass, same pattern as Agent 2).
2. Derivatives: every claim must cite a claim-inventory id; inventory items must quote the confirmed flagship verbatim (substring-verified). Lineage (claim ids per derivative) recorded → `claim_lineage_coverage` = 100% or the asset is withheld.
3. Flagship-first is a state machine, not a convention: fan-out API requires a human confirmation record; attempt without it → refused + `sequencing_violation` alert (spec Alerting).
4. Volume caps and checklist membership checked in code before any generation call.
5. No publish/post/send anywhere: no external channel connector exists (static test, like no-Salesforce/Pardot).

## 3. Model & providers

- Prod: `claude-opus-5`, adaptive thinking effort high, streaming; 32k flagship / 8k per derivative; prompt caching on system prompt + rules pack + brand block + audience pack (stable across the fan-out — biggest cache win in the fleet)
- Dev: Azure GPT via existing provider seam; `response_cost()` prices whatever model answered
- Retry: 3× exp backoff from 2s; truncation → one regenerate with raised ceiling (spec); timeouts 20m flagship / 5m derivative / 45m fan-out run

## 4. Config v0.1.0-draft (pending Marketing Lead sign-off)

- 8 channel recipes verbatim from spec (LinkedIn company w/ hashtag, LinkedIn exec first-person, email hook+CTA, battle card objection-format, one-pager, FAQ/AEO naming LevelShift in answer-extractable text, community non-promotional, service-page section brief)
- Volume caps per derivative type (spec: config, not judgment); regeneration limit 2; self-check rule set = brand rules v0.1.0-draft + terminology/BC-F&O/Copilot/sourcing

## 5. Telemetry & escalation (STS v2, existing emitter)

- Envelope per event; per-asset tokens/cost/latency; outline/pack/rules-pack/claim-inventory versions consumed; self-check results per rule; sections skipped w/ gap-note ids
- Metrics: `flagship_to_fanout_time`, `compliance_first_pass_rate`, `revision_cycles_per_asset`, `claim_lineage_coverage`
- Reason codes: `unsourced_claim`, `selfcheck_failed`, `sequencing_violation`, `truncation_retry`, `outline_divergence`
- Alerts per spec: unsourced competitor/ROI claim in output (must be zero — page AiCoE), fan-out before confirmation, API error rate >5%/15min, repeated truncations, first-pass rate <70%/week

## 6. Bridge + Marketing Studio (same build)

- Bridge: `POST /api/box/{id}/flagship` (trigger on plan confirm — spec "on outline approval"), `GET /api/box/{id}/drafts`, `POST /api/box/{id}/flagship/confirm` (**DEV stand-in for Agent 4, labeled**), fan-out auto-runs on confirm, `POST .../rework`, doc downloads; token middleware unchanged
- Studio Content production tab: flagship card (draft docx link, gap notes, self-check chips, per-model cost) → Content Writer confirm gate → derivative cards appear as fan-out completes; Agent 2's placeholder files replaced by real drafts; live telemetry mirror as today

## 7. Test plan

- Unit per module (mock LLM/transport): intake refuses unverified planned claims; marker/claim-map round-trip; inventory substring-verify rejects paraphrase; fan-out respects checklist + caps; self-check regenerate-then-withhold; rework touches only target asset; staging never overwrites
- Acceptance: one test per spec implementation step 1–10
- Static: fan-out-requires-confirmation-record, no-publish-connector, plane isolation extended
- Gates: pytest green, mypy --strict, ruff, bridge TestClient, npm build; live Azure dev end-to-end (plan → flagship → confirm → fan-out → drafts in studio) before done

## 8. Order of work

1. Models + config + verbatim prompt file
2. intake → flagship+markers → staging (tests each)
3. inventory + grounding verify → fanout seam → selfcheck → rework
4. Orchestration + state machine + CLI + acceptance tests
5. Bridge endpoints + studio Content production wiring → browser verify on Azure dev
6. Docs: agent-spec.md, CHECKLIST, README; deploy notes (Render/Vercel unchanged)

## Open items (not blocking)

- OneDrive/SharePoint access (IT pending) → LocalWorkspace continues; Graph swap later, no code change at call sites
- PostgresContextStore adapter — deferred to OneDrive-lands or Azure phase (decision above)
- Anthropic API key + Batch binding at prod · channel-recipe sign-off by Marketing Lead · Agent 4 replaces the confirm stand-in
