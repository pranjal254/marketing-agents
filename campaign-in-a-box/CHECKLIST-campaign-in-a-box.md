# Acceptance checklist — Agent 2: Campaign-in-a-Box Orchestrator

Verified 2026-09-02 (tests: `tests/test_acceptance_criteria.py` + suites; live Azure
run over the dev bridge).

- [x] Step 1 — non-approved briefs rejected with a structured error
- [x] Step 2 — every intel data point carries source URI + retrieval timestamp;
      unverifiable claims flagged `unverified` and excluded from proof points;
      SemRush failure/no-key → intel-library-only mode, flagged (never silent)
- [x] Step 3 — audience definition with Type 3/4 rationale, personas, exclusions,
      channel emphasis grounded in named sources (events for Type 3/4)
- [x] Step 4 — offer framing with per-claim provenance; brand rules applied
      (deterministic lint + cached prompt block)
- [x] Step 5 — reuse/adapt/create per required asset with evaluated candidates and
      deterministic fitness scores; never `create` without a performed search;
      repository unavailable → create + `reuse_check_pending`, flagged
- [x] Step 6 — outlines for create/adapt assets, seeded from messaging angles,
      planned claims restricted to verified refs
- [x] Step 7 — back-planned calendar honoring the 2-researched-blogs/month rule and
      full-length review gates; constraint chain on every date; workspace from the
      versioned folder/naming template; status tracker initialized; every planned
      asset registered in the Context Store
- [x] Step 8 — pack + plan routed to the Marketing Lead; identity-stamped
      confirmations; deltas produce new versions; infeasible timelines escalate with
      explicit trade-offs (gates never silently compressed)
- [x] Step 9 — completeness diff (missing/extra/version-mismatch) blocks packaging
      with an actionable report; confirmation records verified per asset
- [x] Step 10 — naming validation (auto-correct unambiguous only, flag the rest);
      final-version snapshots as copies; sha256 per packaged asset
- [x] Step 11 — transactional manifest (`packaged_pending_compliance`); partial
      manifests impossible; claim-lineage index included
- [x] Step 12 — gate returns re-open only affected assets; re-hash on re-entry;
      unexplained hash change halts the package (AiCoE)
- [x] Guardrail 1 — unsourced claims excluded, never published (grounding, tested)
- [x] Guardrail 2 — orchestrator never confirms its own outputs (identity required;
      tested)
- [x] Guardrail 3 — repository read-only (no write surface exists; static test)
- [x] Guardrail 4 — only human-confirmed assets enter a package; packaging never
      modifies content
- [x] Guardrail 5 — BC/F&O, Copilot-cloud-only, ShiftAI-one-word, urgency/fear lint;
      no Salesforce/Pardot anywhere (static test); segment-level audience only
- [x] STS v2 telemetry on every action; both agents share one trace per campaign
- [x] Packaging module contains no LLM (static AST test)
- [x] Kill switch + rate breaker guard every Layer-4 action (planning + packaging)
- [x] pytest 52/52 · mypy --strict clean · ruff clean · bridge 13/13 · npm build clean

Pending (tracked in agent-spec §5): SemRush key + AEO endpoint · composition &
brand-rules sign-off by the Marketing Lead · Excel tracker binding at onboarding.
