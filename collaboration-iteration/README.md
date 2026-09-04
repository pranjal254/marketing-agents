# Agent 4 — Content Collaboration & Iteration

The review-cycle manager between staged drafts and content-confirmed: assigns
reviewers from the workflow plan, consolidates fragmented feedback into one
de-duplicated instruction set, applies agreed textual edits as tracked new
versions (claim markers protected in code), holds conflicts for the Marketing
Lead (never adjudicates), routes structural rework to Agent 3, and keeps every
asset's iteration state measurable. `content_confirmed` is a human action —
structurally, not by convention.

- Spec: `../LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 4
- Binding decisions: `agent-spec.md` · plan: `../PLAN-collaboration-iteration.md`
- Verification record: `CHECKLIST-collaboration-iteration.md`

## Run locally

Via the bridge (recommended — plays Execution Studio's event role):

```
cd Agents
.venv\Scripts\python -m uvicorn c2c_bridge.app:app --port 8787
```

Endpoints: `POST .../assets/{id}/feedback` · `POST .../assets/{id}/feedback-complete`
· `POST .../assets/{id}/conflicts/{cid}/resolve` · `POST .../assets/{id}/confirm`
(the human gate) · `GET .../review` · `POST .../sweep`. The studio's Content
production tab drives all of them per draft card.

## Gates

```
cd Agents/collaboration-iteration
..\.venv\Scripts\python -m pytest -q      # 32 tests, all external calls mocked
..\.venv\Scripts\python -m mypy           # strict
..\.venv\Scripts\python -m ruff check .
```
