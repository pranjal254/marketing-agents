# Build & verification record — Agent 3 (Content Repurposing)

Built 2026-09-03 per the approved `Agents/PLAN-content-repurposing.md`.

## Gates (all green at delivery)

| Gate | Result |
|---|---|
| `pytest` content-repurposing | 55 passed (unit + acceptance per spec steps 1–10 + static guardrails) |
| `pytest` bridge | 23 passed (incl. 5 Agent 3 lifecycle tests over HTTP) |
| `pytest` shared / agent 1 / agent 2 | 63 / 56 / 52 passed (no regressions) |
| `mypy --strict` | clean ×5 packages |
| `ruff` | clean ×5 packages |
| `npm run build` (studio) | clean (tsc + vite) |

## Acceptance per spec implementation step

1. Claims validated vs verified proof points; unverifiable sections refused → gap notes (`test_step_01`, live-verified)
2. Flagship drafted from outline in brand voice, self-check passing (`test_step_02`)
3. Inline `[c-N]` markers + provenance table + sidecar claim map (`test_step_03`)
4. Versioned docx staged; fan-out impossible pre-confirmation (`test_step_04`)
5. Claim inventory extracted from the CONFIRMED flagship, quotes verbatim-verified (`test_step_05`)
6. Channel-native derivatives per recipe; FAQ names LevelShift (AEO rule) (`test_step_06`)
7. Volume caps + checklist membership; reuse assets skipped (`test_step_07`)
8. Generation-time self-check per asset, regenerate ≤2 then withhold (`test_step_08`)
9. Drafts registered with claim lineage + canonical naming (`test_step_09`)
10. Rework regenerates only the affected asset, new version, others untouched (`test_step_10`)

## Static guardrails (tests over the package source)

- No publish/post/send surface: no HTTP/mail/social/Salesforce import exists
- No destructive file operations; every workspace write flows through the
  additive protocol via `orchestration._upload_once` only
- The agent never calls its own `confirm_flagship`
- The versioned system prompt is the spec text verbatim

## Live verification (Azure dev, 2026-09-03, isolated bridge :8788 + studio :5174)

API loop: request → approve → Agent 2 plan → pack/plan confirm → **flagship
staged (8 sections, 3 verified markers, self-check pass, 1 genuine gap note)** →
fan-out pre-confirm **refused 409 + sequencing_violation escalation** → human
confirm → **fan-out: 4 derivatives staged with lineage from a 14-item
verbatim-verified inventory; the reuse FAQ correctly skipped** → per-asset
confirms registered the REAL Agent 3 bytes → **manifest v1 (6 assets) with the
claim-lineage index**. Telemetry priced the responding model
(`gpt-5.4-nano-2026-03-17`) at its own rates, `span_incremental`.

Browser loop (the user's path): intake describe → live gap round → send for
approval → BU Lead approve (auto-triggers planning) → plan-confirm gate →
**flagship auto-triggered on confirmation** → Content production tab showed
flagship staged w/ gap notes → **Confirm flagship content** → fan-out ran →
**flagship + 5 derivative cards with titles, versions, lineage and an 18-claim
inventory**; Activity mirrored real CR events. Duplicate-detection guardrail also
fired live (Agent 1 escalated a near-identical request — expected behavior).

## Known dev stand-ins (labeled in code/UI)

- Flagship confirm + per-asset confirm: studio writer gate stands in for Agent 4
- Word comments: marker table in docx + sidecar claim map until Graph binding
- Fan-out execution: sequential independent calls behind the seam; Anthropic
  Message Batches API binds at production

## Outstanding (not blocking)

Channel-recipe sign-off (v0.1.0-draft, Marketing Lead) · OneDrive/SharePoint
access (IT) · Anthropic key + Batch binding at prod · Postgres ContextStore
adapter (deferred per plan §0) · Agent 4 replaces the confirm stand-ins
