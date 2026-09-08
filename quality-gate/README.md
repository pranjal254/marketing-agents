# Quality Gate & Approval Agent (Agent 5)

Phase 1's terminal agent: gates the Campaign-in-a-Box package manifest, routes
the retained human review gates, records the approval chain, locks approved
versions, and releases the approved package reference for Phase 2.

## Shape (spec: "Hybrid — rules engine + AI + deterministic routing")

1. **Deterministic pass** (`deterministic.py`, no LLM): the shared brand rules
   pack lint (terminology, banned terms, urgency/fear), naming convention, AEO
   named-mention presence, and inline `[c-N]` claim-marker resolution.
2. **Contextual pass** (`generation.py`, claude-sonnet-5, 8k/asset): exactly five
   meaning-level rules — BC/F&O independence in substance, Copilot scope, claim
   sourcing, tone, brand voice (advisory). The model must NAME each rule it
   completed; a missing rule fails the asset closed. Severity is reassigned by
   policy in code (`grounding.py`) — model severity is never trusted, invented
   rules are dropped, quotes are verified verbatim.
3. **Routing module** (`routing.py`, no LLM): distribution class by policy lookup
   (public/internal), sequenced review tasks (Grammar QA → BU Lead package
   sign-off), SLA reminders at 50%/90% + escalations, structural sequence
   integrity (`ordered_gate_open`).
4. **Lock** (`locking.py`): approved versions are hash-verified and locked
   read-only IN PLACE (packaging already snapshotted into `final/`) — this
   package has ZERO workspace write surface (static-tested).

## Guardrails in code

- Flags and blocks, never edits; remediation is description only.
- `record_review_outcome` / `record_false_negative` are human-only: no code path
  in this package invokes them (static-tested); identity is mandatory.
- Verdict = pure function of blocking findings + check completeness.
- Fail-closed everywhere: hash mismatch, unreadable snapshot, contextual error,
  unfinished rule → fail; lock failure blocks release; post-lock modification
  invalidates the package (`verify_locks`).
- All rules cite the versioned config (`config/quality_gate.json`) and brand
  pack; the agent adds no rules of its own.

## Notes

- STS v2 `agent.type` is `decision` (the kit taxonomy has no "hybrid"); the
  business config keeps the spec's `hybrid` label.
- Record kinds registered in migration `0004_capability_c2c_agent5.sql`
  (governance CI enforces the catalog).
- Signals (`assets_failed`, `package_returned`, `package_approved`) bind in the
  bridge (dev) / Execution Studio (prod): failures → Agent 4 with findings +
  packaging re-open; returns → packaging re-open with reviewer notes verbatim;
  approval → locked package reference persisted for Phase 2. Nothing publishes.

## Run

```bash
pytest -q            # 37 tests
mypy src             # strict
ruff check .
python -m c2c_quality_gate.cli run <campaign_id>
```
