# C2C Agent Bridge (dev)

Local HTTP + SSE bridge that plays ShiftAI Execution Studio's role on a laptop so the
Marketing Studio UI can drive the real agents and watch their STS v2 telemetry
stream live: **Live agents** (Agent 1, Campaign Identification) and **Campaign box
(live)** (Agent 2, Campaign-in-a-Box Orchestrator). One session = one store, one
telemetry bus, one kill switch — an approved brief flows straight from Agent 1 into
Agent 2's planning pass on the same trace. Dev tool only — production
invocation/task routing belongs to Execution Studio.

## Run

```powershell
cd Agents
.venv\Scripts\python -m uvicorn c2c_bridge.app:app --port 8787
```

Provider/credentials come from `Agents/.env` (`LLM_PROVIDER=azure_openai` in dev,
`mock` for offline — mock abstains by design). Working data lands in
`bridge/.bridge-run/` (context store, telemetry.jsonl, workspace with the brief
.docx files); delete the folder to reset.

Then start the UI:

```powershell
cd marketing-studio
npm run dev        # → http://localhost:5173/live  (sidebar: "Live agents")
```

`VITE_LIVE_API` overrides the bridge URL (default `http://localhost:8787`).

## Endpoints

| Method/Path | Purpose |
|---|---|
| `GET /api/health` · `GET /api/meta` | status, provider/model, config, taxonomy |
| `POST /api/requests` `{source, request, hold_for_verification?}` | run the agent; `hold_for_verification` keeps the draft with the requester (`draft_review`) instead of routing — the AI-first intake flow |
| `POST /api/cases/{id}/answers` `{answers, actor_id}` | requester gap answers → agent resumes |
| `POST /api/cases/{id}/decision` `{decision, actor_id, notes?}` | BU Campaign Lead gate: approve / reject / **returned** (back to the requester with feedback; 409 on gate violations) |
| `POST /api/cases/{id}/revise` `{directive, aspects[], actor_id}` | requester iteration round: the agent rewrites objective/topic per the directive (audited human_gate) |
| `POST /api/cases/{id}/release` `{actor_id}` | requester verification: routes a held draft (`draft_review`) to the BU gate |
| `GET /api/cases` · `GET /api/cases/{id}` | case list / detail (brief, gaps, approval task) |
| `GET /api/telemetry?after=SEQ` | recent STS records (polling fallback) |
| `GET /api/stream` | SSE live feed of every STS record (`bridge.seq` ordering) |
| `GET /api/documents/{name}` | download a generated brief .docx |
| `POST /api/control/kill-switch` `{paused, reason}` | pause/resume EVERY agent in the session (governance demo) |
| `POST /api/control/reset` | fresh dev session (new workdir; prior session data stays on disk, nothing deleted) |

### Agent 2 — Campaign-in-a-Box (`/api/box/…`)

| Method/Path | Purpose |
|---|---|
| `POST /api/box/campaigns/{cmp}/plan` `{actor_id}` | planning pass from the approved brief (reuses Agent 1's trace) |
| `POST /api/box/campaigns/{cmp}/confirm` `{kind: pack\|plan, decision: confirmed\|modified, actor_id, deltas?}` | Marketing Lead gate; deltas → new version |
| `POST /api/box/campaigns/{cmp}/assets/{asset}/confirm` `{actor_id, text?, claim_refs?}` | **DEV stand-in for Agents 3–4**: registers a content-confirmed asset with its human confirmation record |
| `POST /api/box/campaigns/{cmp}/package` | deterministic packaging run (blocks on any completeness gap) |
| `POST /api/box/campaigns/{cmp}/reopen` `{asset_ids, requesting_gate, actor_id}` | gate return: re-open only the named assets |
| `GET /api/box/campaigns` · `GET /api/box/campaigns/{cmp}` | plan list / full detail (pack, checklist, outlines, plan, manifest, report) |
| `GET /api/box/documents?path=REL` | download pack .docx / tracker .csv / final snapshots (workspace-scoped) |

Dev seed data: each session gets a small synthetic content repository +
intel-library (`c2c_bridge/seed.py`) so reuse search and intel gathering have real
material. No SemRush key → intel-library-only fallback, flagged per spec.

The human gates stay human: the bridge only carries the requester's answers and the
approver's explicit identity-stamped decision into the agent — there is no endpoint
that advances a brief any other way.

## Tests

```powershell
cd Agents\bridge
..\.venv\Scripts\python -m pytest tests -q   # TestClient + mock provider, no network
```
