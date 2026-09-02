# C2C Agent Bridge (dev)

Local HTTP + SSE bridge that plays ShiftAI Execution Studio's role on a laptop so the
Marketing Studio UI (**Live agents** screen) can drive the real Campaign
Identification agent and watch its STS v2 telemetry stream live. Dev tool only —
production invocation/task routing belongs to Execution Studio.

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
| `POST /api/control/kill-switch` `{paused, reason}` | pause/resume the agent (governance demo) |
| `POST /api/control/reset` | fresh dev session (new workdir; prior session data stays on disk, nothing deleted) |

The human gates stay human: the bridge only carries the requester's answers and the
approver's explicit identity-stamped decision into the agent — there is no endpoint
that advances a brief any other way.

## Tests

```powershell
cd Agents\bridge
..\.venv\Scripts\python -m pytest tests -q   # TestClient + mock provider, no network
```
