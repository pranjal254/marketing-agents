# Agent Spec — Content Repurposing Agent (Agent 3)

Filled from `levelshift-agent-starter-kit/docs/agent-spec-template.md`.
Authoritative source: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 3 + V2 Cross-Agent Standards A–D.

## Identity

| | |
|---|---|
| Agent ID | `content_repurposing` |
| Agent type (STS enum) | `decision` (AI Agent) |
| Process | `content-to-campaign` (Phase 1, sub-process 2: Content Drafting) |
| Model (production) | `claude-opus-5`, adaptive thinking effort high, streaming |
| Model (dev/test) | Azure OpenAI deployment behind the shared `LLMProvider` seam; unit tests use the mock provider |
| Max tokens | 32,000 flagship · 8,000 per derivative |
| Timeouts | 20 min flagship · 5 min/derivative · 45 min full fan-out (spec) |
| Risk tier / data class | medium / confidential (pre-release marketing content; no customer PII) |
| System prompt | `prompts/content-repurposing.system.v1.0.0.md` — spec verbatim, versioned |
| Config | `config/content_repurposing.json` v0.1.0 (recipes **0.1.0-draft**, pending Marketing Lead sign-off) |

## What it does (create-once model)

1. **Flagship first** (steps 1–4): loads the approved outline + audience & offer
   pack + checklist from the Context Store (written by Agent 2; drafting is
   possible only once pack AND plan are confirmed — "on outline approval").
   Outline sections whose planned claims don't resolve to verified proof points
   are refused up front and become gap notes. The Opus draft carries inline
   `[c-N]` claim markers; grounding strips any section citing outside the
   verified set; the deterministic self-check (brand lint + unsourced-numeric +
   AEO named-mention) regenerates at most `maxRegenerations` times, then
   withholds. Passing drafts are staged as versioned Word documents (marker
   provenance table + sidecar claim-map JSON) in the campaign workspace.
2. **Human gate**: `confirm_flagship` records the identity-stamped
   content-confirmed decision. In production it is carried by the Content
   Collaboration Agent (Agent 4); in dev the studio's writer gate stands in
   (labeled). **No code path in this package calls it** — enforced by a static
   test.
3. **Fan-out** (steps 5–9): only from a human-confirmed flagship (state machine;
   violation → refused + `sequencing_violation` escalation). The claim inventory
   is LLM-extracted then **verbatim-verified in code** (non-matching quotes
   dropped; degrade → deterministic inventory from the flagship claim map —
   sourced by construction). Derivatives per channel recipe, ONLY for checklist
   create/adapt assets, volumes from the checklist (config, not judgment),
   lineage (inventory claim ids) recorded per draft, unsourced numerics →
   withhold. Stage the passing subset; gap-note the rest (spec Fallback).
4. **Rework** (step 10): regenerates ONLY the affected asset as a new version;
   flagship rework is possible only pre-confirmation (afterwards it would
   invalidate the inventory — governance re-open path instead).

## Inputs / outputs (Context Store contract)

| Direction | Kind | Notes |
|---|---|---|
| in | `plan_case`, `audience_offer_pack`, `asset_checklist`, `content_outlines` | written by Agent 2 |
| out | `repurpose_case`, `staged_draft` (versioned), `claim_inventory`, `content_gap_note`, `failed_repurpose_run` | consumed by Agents 4–5 + the bridge |

Workspace: the SAME campaign workspace Agent 2 created; drafts land in
`{campaign}/drafts` (additive protocol — no delete/move/overwrite exists);
Agent 2's confirmed intake copies live in `{campaign}/confirmed`.

## Guardrails (all enforced in code, tested)

1. Sourced claims only — unmarked/unresolvable claims never survive grounding; gap notes, never plausible prose.
2. Full rules-pack compliance at generation time (shared brand lint; errors regenerate-then-withhold).
3. Flagship-first sequencing is a state machine + static test, not a convention.
4. Volumes capped by the checklist; only checklist assets generated; reuse assets skipped.
5. No publish/post/send surface exists (static test: no HTTP/mail/social import in the package).

## Telemetry (STS v2, schema-validated at emit)

- `decision_made` per LLM call: request vs responding model, tokens, cache reads,
  cost priced by the RESPONDING model's rate card (`span_incremental`), prompt
  template id/version (`content-repurposing-{flagship,inventory,derivative}` v1.0.0),
  self-check attempt, truncation-retry flag.
- `human_gate` (flagship confirm, rework requests), `case_escalated`
  (`policy_gap` + precise `shiftai.learn.reason_code`), `action_taken`
  (stage_draft w/ lineage), `run_summary` with fan-out metrics
  (`shiftai.fanout.staged/withheld/skipped/claim_lineage_coverage`).
- Spec metrics coverage: `flagship_to_fanout_time` (human_gate → run_summary),
  `compliance_first_pass_rate` (self-check attempts), `claim_lineage_coverage`.

## Cost model (Cross-Agent Standard A)

Prompt caching on the system prompt + brand rules + channel recipes — identical
cacheable blocks across the flagship call and every fan-out call. Fan-out runs as
independent per-derivative calls behind the `run_fanout_jobs` seam; the production
Anthropic binding replaces the walker with the **Message Batches API** (50% off)
with no caller change. Azure OpenAI batch (~24 h) was rejected — it cannot meet
the 20-minute fan-out SLA.

## Dev bindings / production swap

| Concern | Dev | Production |
|---|---|---|
| LLM | Azure OpenAI (env-selected) | Anthropic `claude-opus-5` + Batch API fan-out |
| Workspace | `LocalCampaignWorkspace` | OneDrive via Graph (same protocol) |
| Confirm signal | studio writer gate (labeled stand-in) | Content Collaboration Agent (Agent 4) |
| Store | SQLite `ContextStore` | Postgres adapter (deferred per plan §0) |
| Word comments | marker table in docx + sidecar JSON | real Word comments via Graph |
