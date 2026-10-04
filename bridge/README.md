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

### Content settings (`/api/box/…`)

How many of each asset a campaign needs and how long each should run. The
Content Writer sets these between confirming the flagship and triggering the
fan-out; generation reads them, falling back to the plan volumes and the config
word ranges when nothing is saved.

| Method/Path | Purpose |
|---|---|
| `GET /api/box/campaigns/{cmp}/content-settings` | current settings plus the bounds the UI must respect; serves config defaults until something is saved |
| `PUT /api/box/campaigns/{cmp}/content-settings` `{actor_id, actor_role, note?, items[]}` | identity-stamped save; a **patch**, so assets left out keep their values |

`items[]` entries are `{asset_id, variants?, min_words?, max_words?}`. Anything
beyond the configured ceiling is clamped rather than rejected, and every clamp
comes back in `settings.adjustments` so the writer sees the correction. The
ceiling and the word floor/ceiling live in the agent's versioned config
(`contentLimits` in `content-repurposing/config/*.json`), which is also where
the per-asset-type default ranges are tuned. Each save is a new version.

### Aggregated telemetry export (`/api/telemetry/export`)

A dashboard-ready rollup of the STS stream for the ShiftAI Execution Studio, so
another application can read cost, latency, escalations and human gates broken
down by **journey step** without replaying raw records.

| Method/Path | Purpose |
|---|---|
| `GET /api/telemetry/export` | the banked snapshot, served from the context store; cheap to poll, never recomputes |
| `POST /api/telemetry/export/refresh[?force=true]` | recompute and bank a new one; rate limited |
| `GET /api/telemetry/export/history` | when previous snapshots were taken, and how much each held |

Refreshes are limited to one per `TELEMETRY_EXPORT_INTERVAL_HOURS` (default 6).
Calling sooner is **not an error**: the response is the banked snapshot with
`refreshed: false` and a `next_refresh_at`, so a polling client needs no special
case. `?force=true` overrides it.

Each refresh aggregates only the raw records after the previous snapshot cursor
and merges that delta onto the banked figures. This matters because the raw
stream is a per-session JSONL file that does not survive a restart, while
snapshots live in the context store (Postgres when `DATABASE_URL` is set). Totals
therefore only move forwards. Every figure is a counter, a sum, a maximum or a
set union, so the merge is exact; averages are derived at read time from a sum
and a count rather than stored.

The payload ships the journey vocabulary (`process.stages`) alongside the
figures, so a consumer can render steps it has never seen without hardcoding
them. Breakdowns: `by_stage`, `by_agent`, `by_event_type`, `by_model`,
`by_outcome`, `by_escalation_reason`, `by_human_gate_decision`.

Stage context comes from the versioned journey pack
(`shared/src/shiftai_shared/process/`). Each agent declares the step it sits in;
agents spanning several steps stamp the exceptions (the audience and offer pass
is step 2, packaging is step 6, grammar QA is step 8, sign-off is step 9).

### Ask anything (`/api/ask`): Beta, read-only

| Method/Path | Purpose |
|---|---|
| `GET /api/ask/meta` | model, version, the tool catalogue, and what the assistant can and cannot do |
| `POST /api/ask` `{question, conversation_id?, viewer?, actor_id?}` | an answer, the lookups behind it, and the conversation it belongs to |
| `GET /api/ask/conversations?actor_id=` | past conversations, newest first |
| `GET /api/ask/conversations/{id}` | one conversation in full |

The assistant answers by **looking things up**. Each turn runs a short loop:
the model either calls one of the read-only tools in `ask_tools.py` or produces
its answer, and tool results feed the next step. Passing `conversation_id`
continues a thread, so a follow-up like "check the gate for it" resolves
against what was already said.

Tool calling is a **JSON protocol, not a provider feature**. The shared
`LLMProvider` interface is deliberately provider-agnostic and native tool
calling differs between Anthropic and Azure OpenAI, so the model replies with a
strict JSON object (`{"tool", "args"}` or `{"answer", "references"}`) exactly as
every agent here already does, and the loop dispatches it. The assistant
therefore works unchanged on whichever provider the fleet runs.

Budget: at most `MAX_STEPS` (6) tool calls and `MAX_COST_USD` (0.50) per
question. A loop that cannot end is worse than a wrong answer, because it burns
money quietly. Running out of steps produces an honest "I ran out of lookups",
never a guess.

**Nothing in the toolset writes, and none of it invokes an agent.** The registry
is an allowlist, not a filter over a larger surface: a capability absent from it
cannot be invoked however the model phrases the request. Every gate-advancing
path is identity-stamped and sequenced, and a chat turn is neither, so an answer
that wants an action names the screen that performs it.

**The access story is weaker than the first cut, deliberately.** The original
single-shot version could only read the snapshot the browser sent, so it
structurally could not exceed the caller's session. These tools read the context
store directly, which is what makes the assistant useful and also means it can
reach any campaign in the workspace. Until SSO lands, treat the assistant as
having the reach of the workspace, not of the person asking.

Conversations persist in the context store (`assistant_conversation`), so they
survive a browser, a device and a restart. The studio keeps a localStorage copy
as a cache for instant reopen and a sessionStorage key for which thread a tab
has open; the database is the record.

Telemetry: one `decision_made` per reasoning step, one `tool_execution` per
lookup (with `gen_ai.tool.name`), and a `run_summary` per question. All of it is
stage-free, because the assistant sits beside the journey rather than inside it
and must not distort per-step cost. Question text never reaches the stream.

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
