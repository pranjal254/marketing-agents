# Agent 2 — Campaign-in-a-Box Orchestrator (build notes)

Spec: `LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 2
(authoritative). Plan: `Agents/PLAN-campaign-in-a-box.md` (user-approved 2026-09-02).

## §1 What is implemented

All 12 spec implementation tasks:

| Spec step | Where |
|---|---|
| 1 approved-brief-only intake | `intake.py` (`BriefNotApprovedError` structured rejection) |
| 2 sourced intel + provenance | `intel.py` + `shiftai_shared.semrush` (fallback → intel-library-only, flagged) |
| 3–4 audience & offer pack | `planning.py` call 1 + `grounding.ground_pack` (unsourced → excluded + gaps) |
| 5 reuse checklist | `repository.py` (deterministic fitness scores) + `planning.py` call 2 + `grounding.ground_reuse_items` |
| 6 outlines | `planning.py` call 2 + `grounding.ground_outlines` (claims limited to verified refs) |
| 7 back-planned calendar + workspace + registry | `calendar.py` (pure date math) + `workspace.py` + `persistence.register_planned_asset` |
| 8 confirmation gate + deltas + infeasibility escalation | `orchestration.confirm` / `calendar` infeasibility report with explicit trade-offs |
| 9 completeness diff | `packaging/completeness.py` — non-empty diff blocks, never padded/trimmed |
| 10 naming + snapshots + hashes | `packaging/naming.py` (auto-correct unambiguous only) + `packaging/snapshot.py` (sha256) |
| 11 transactional manifest | `orchestration.run_packaging` — plan (read+hash, zero writes) then commit; manifest registers only after every snapshot landed |
| 12 rework re-open + re-hash | `orchestration.reopen_assets`; unexplained hash change on re-entry halts (AiCoE) |

Packaging is LLM-free by construction — enforced by a static AST test
(`test_packaging_module.py::test_packaging_module_never_imports_llm_or_provider`).

## §2 Model & providers

- Production: `claude-opus-5`, 16k output cap, planning timeout 20 min, packaging
  5 min (spec). Prompt caching on the three stable system blocks: versioned spec
  system prompt (`prompts/campaign-in-a-box.system.v1.0.0.md`, verbatim), the brand
  rules pack block, the composition/config block.
- Dev/test: `azure_openai` (gpt-5.4-nano) / `mock` behind the shared provider
  interface — verified live end to end on Azure 2026-09-02.

## §3 Business Capability config (v0.1.0, composition 0.1.0-draft)

`config/campaign_in_a_box.json` — **the composition (9 asset types) uses only spec
vocabulary and is pending Marketing Lead sign-off**, as are: drafting-day estimates,
fitness weights/thresholds, thin-intel threshold (30%), urgency/fear lint terms.
Review gates (flagship 3bd / derivative 2bd) come from the spec's Agent 4 SLAs;
capacity rule (2 researched blogs/month) from the spec.

## §4 Brand rules pack

`shiftai_shared.brand` — versioned rules pack v0.1.0-draft derived from the
marketing-owned documents in `reference/brand-guidelines/` (source files git-ignored;
the extracted pack is the committed artifact). Consumed as a cached prompt block and
as the deterministic `lint_text` check (flags, never edits). **The playbook's
banned-phrases section is a placeholder — `bannedTerms` stays empty until Marketing
provides the real list.**

## §5 Approved conflict resolutions / open items

1. **AEO/LLM-citation data (spec step 2)**: SemRush has no stable public endpoint for
   it at build time. Not implemented — the data points are absent, never fabricated.
   Revisit when SemRush exposes the AI toolkit API (or IT provides the contract).
2. **SemRush key**: none yet (user decision 2026-09-02) — dev runs the spec's
   documented fallback (intel-library-only, flagged in pack + telemetry).
   `SEMRUSH_API_KEY` env activates the live client with zero code change.
3. **Status tracker binding**: dev materializes the tracker as versioned CSV files
   through the additive Workspace protocol; the production binding moves to the Excel
   tracker workbook via Graph at Execution Studio onboarding.
4. **Snapshot revert**: agents never delete (guardrail), so a failed packaging commit
   reverts *state* and records landed snapshot refs; the idempotency store resumes
   them on retry — a partial manifest remains impossible either way.
5. **STS enum mapping** (same pattern as Agent 1): deterministic escalations carry
   `escalation.reason=policy_gap`; the precise code (`thin_intel`,
   `infeasible_timeline`, `completeness_block`, `hash_mismatch`, …) rides in
   `shiftai.learn.reason_code`.
6. **Confirmed-asset input**: production receives `confirmed_assets` from the Content
   Collaboration Agent (Agent 4). Until it exists, the dev bridge exposes a clearly
   labeled stand-in endpoint that registers a content-confirmed asset with its human
   confirmation record. No production code path can confirm content.
7. **Windows MAX_PATH (dev)**: campaign slugs cap at 24 chars so workspace paths stay
   inside 260 chars in dev sessions; workspace writes raise typed
   `WorkspaceWriteError` on OS failures.

## §6 Verification (2026-09-02)

- 52 package tests + 13 bridge tests green; mypy --strict clean; ruff clean.
- Live on Azure dev: approve brief (Agent 1) → planning pass (grounded pack,
  unverified_share 0, 6-asset checklist with adapt decisions citing scored
  candidates, 6 outlines, feasible back-plan) → pack+plan confirmed → 6 assets
  content-confirmed (bridge stand-in) → packaging → manifest v1 with sha256 per
  asset → snapshot + pack .docx downloads over HTTP. Both agents share one trace
  per campaign (journey reconstruction).
