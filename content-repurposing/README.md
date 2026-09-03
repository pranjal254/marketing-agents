# Agent 3 — Content Repurposing

The production engine of Phase 1: one flagship asset per campaign, drafted from
the approved outline and audience & offer pack with sourced claims only, then —
after a human confirms it — up to eight channel-native derivatives generated from
the flagship's verbatim-verified claim inventory, with claim lineage per draft.

- Spec: `../LevelShift-Content-to-Campaign-Phase1-Agent-Technical-Specs-V2.1` §Agent 3
- Binding decisions: `agent-spec.md` · plan: `../PLAN-content-repurposing.md`
- Verification record: `CHECKLIST-content-repurposing.md`

## Run locally

Via the bridge (recommended — plays Execution Studio):

```
cd Agents
.venv\Scripts\python -m uvicorn c2c_bridge.app:app --port 8787
```

Endpoints: `POST /api/box/campaigns/{id}/flagship` · `GET .../drafts` ·
`POST .../flagship/confirm` (human gate) · `POST .../fanout` · `POST .../rework`.
The Marketing Studio's Content production tab drives all of them.

Via the CLI against an existing bridge session directory:

```
.venv\Scripts\python -m c2c_content_repurposing.cli flagship <workdir> <campaign_id>
.venv\Scripts\python -m c2c_content_repurposing.cli confirm  <workdir> <campaign_id> <your_email>
.venv\Scripts\python -m c2c_content_repurposing.cli fanout   <workdir> <campaign_id>
```

## Gates

```
cd Agents/content-repurposing
..\.venv\Scripts\python -m pytest -q      # 55 tests, all external calls mocked
..\.venv\Scripts\python -m mypy           # strict
..\.venv\Scripts\python -m ruff check .
```
