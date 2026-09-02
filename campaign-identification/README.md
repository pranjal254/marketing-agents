# Campaign Identification Agent (Agent 1)

LevelShift Content-to-Campaign Phase 1. Turns campaign requests (intake form /
quarterly plan / event calendar) into validated, classified, human-approved campaign
briefs (Word doc + structured record). Model: **claude-sonnet-5**; dev/test runs use
**Azure OpenAI** behind the same provider interface. Built on the starter-kit
architecture; shared mechanics live in `../shared` (`shiftai_shared`).

## Layout

```
config/campaign_identification.json    Business Capability config (versioned, read-only)
prompts/campaign-identification.system.v1.0.0.md   spec system prompt, verbatim
src/campaign_identification/           intake → validation → rules → conflicts →
                                       classify (L3) → gaps → brief → approval →
                                       persistence → orchestration → cli
tests/                                 unit + e2e + acceptance-criteria suites (all mocked)
agent-spec.md · CHECKLIST-…md          governance artifacts
```

## Setup (dev)

```powershell
cd Agents
py -3.13 -m venv .venv            # 3.12+ required; 3.13 fine
.venv\Scripts\python -m pip install -e .\shared -e .\campaign-identification
```

## Environment variables

Template: [`../.env.example`](../.env.example) — `copy ..\.env.example ..\.env`, fill
values in `.env` (git-ignored; never commit or share it). Real env vars override `.env`.

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic` (prod) · `azure_openai` (dev) · `mock` (offline) |
| `ANTHROPIC_API_KEY` | prod only |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` / `AZURE_OPENAI_API_VERSION` | dev provider |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` | Microsoft Graph (Forms-workbook/Excel/OneDrive/Word), client-credential flow |
| `SHIFTAI_ENVIRONMENT` | `dev` (default) · `staging` · `production` — stamped on telemetry |
| `SHIFTAI_TENANT_ID` | default `levelshift-internal` |
| `STS_SCHEMA_PATH` | optional override; defaults to the sibling starter-kit schema |

## Run it (dev CLI — production is Execution Studio-triggered)

```powershell
$env:LLM_PROVIDER="azure_openai"   # dev; "mock" = no-network, but the bare mock
                                   # abstains by design → cases escalate (fail-safe).
                                   # For a scripted offline happy path run:
                                   #   ..\.venv\Scripts\python samples\demo_end_to_end.py
cd campaign-identification
..\.venv\Scripts\python -m campaign_identification.cli process --request samples\sample-request-complete.json
# → status awaiting_approval, brief .docx in .dev-run\workspace\, telemetry in .dev-run\telemetry.jsonl

..\.venv\Scripts\python -m campaign_identification.cli process --request samples\sample-request-incomplete.json
# → status awaiting_input + targeted gap questions
..\.venv\Scripts\python -m campaign_identification.cli answer-gaps --case case_XXXX --answers answers.json --actor-id requester@levelshift.com

..\.venv\Scripts\python -m campaign_identification.cli approve --case case_XXXX --actor-id bu.lead@levelshift.com
# → human_gate + case_resolved + run_summary; campaign registered in the calendar
```

## Quality gates

```powershell
..\.venv\Scripts\python -m pytest ..\shared\tests tests -q     # 92 tests
..\.venv\Scripts\python -m mypy                                # strict, clean
..\.venv\Scripts\python -m ruff check .. ; ..\.venv\Scripts\python -m ruff format --check ..
```

## Execution Studio onboarding notes

1. **Triggers:** wire Forms-workbook new-row, plan-sheet new-row, calendar flag, and
   watched-folder events to `process_request`; approvals/gap answers arrive as task
   completions calling `record_human_decision` / `submit_gap_answers` (the CLI is the
   dev stand-in for exactly these entry points).
2. **Bindings to swap:** `ContextStore` (SQLite → Studio store), `Workspace`
   (LocalWorkspace → `OneDriveWorkspace` with drive/folder IDs), `TelemetrySink`
   (JSONL → Studio STS ingestion), idempotency store, `LLM_PROVIDER=anthropic`.
3. **Human gates:** BU Campaign Lead approval is structural — the platform must route
   the `approval_task` records; the agent never advances a brief on its own.
4. **Telemetry:** STS v2.0.0, validated at emit; journey reconstructs from
   `shiftai.trace.id`; `shiftai.learn.*` on human_gate feeds the Standard C/D loops.
5. **Secrets:** Studio secret store → env vars above; nothing is read from files.
6. Deferred platform-side items are listed in `agent-spec.md` §10 and the CHECKLIST.
