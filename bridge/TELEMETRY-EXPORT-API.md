# Content to Campaign: telemetry export API

For the team building the dashboard application. This is the contract for
reading aggregated agent telemetry out of the Content to Campaign bridge.

Everything here was captured from a live response, not written from the source.

---

## 1. What this gives you

A dashboard-ready rollup of the agent fleet's activity, already aggregated, so
you never replay the raw event stream. Cost, volume, latency, escalations and
human approvals, broken down by:

- **journey step** (the nine business steps a marketer recognises)
- **agent** (the five agents plus the studio assistant)
- **event type**, **model**, **outcome**, **escalation reason**, **human gate decision**

The headline capability is the journey-step breakdown. It answers "where does
our AI spend actually go" in business terms rather than agent internals.

## 2. Endpoints

Base URL is the bridge. Production today is
`https://c2c-agent-bridge.onrender.com`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/telemetry/export` | the banked snapshot. Cheap, never recomputes. **Poll this.** |
| `POST` | `/api/telemetry/export/refresh` | recompute and bank. Rate limited. |
| `GET` | `/api/telemetry/export/history` | when previous snapshots were taken and how much each held |

### Authentication

Bearer token on every call:

```
Authorization: Bearer <BRIDGE_API_TOKEN>
```

Ask AiCoE for the token. Without it every route except `/api/health` returns
`401`.

### The refresh contract

`POST /api/telemetry/export/refresh` is the trigger. It is limited to **one
refresh per six hours** (configurable via `TELEMETRY_EXPORT_INTERVAL_HOURS`).

Calling sooner is **not an error**. You get `200` with the banked snapshot and:

```json
{ "refreshed": false, "records_added": 0, "next_refresh_at": "2026-10-05T17:40:53Z",
  "reason": "last refreshed at 2026-10-05T11:40:53Z; refreshes are limited to one per 6 hours" }
```

So a polling client needs no special case: read `refreshed` if you care, read
`snapshot` either way.

Query parameters:

- `?force=true` ignores the rate limit. For operators and tests.
- `?rebuild=true` discards the banked figures and recomputes from the raw
  stream alone. The raw stream is per-session, so a rebuild usually returns a
  **smaller** total rather than a corrected one. Only use it to supersede a
  snapshot you know to be wrong.

**Suggested integration:** poll `GET /export` on whatever cadence your
dashboard refreshes, and call `POST /refresh` on a six-hourly schedule.

## 3. Response shape

```jsonc
{
  "process": {            // the vocabulary, so you can render steps you have never seen
    "name": "content-to-campaign",
    "version": "1.0.0",
    "label": "Content to Campaign",
    "description": "...",
    "stages": [ { "stage_id": "intake", "ordinal": 1, "label": "Intake", "description": "..." } ]
  },
  "refresh_interval_hours": 6.0,
  "next_refresh_at": "2026-10-05T17:40:53Z",
  "banked": true,         // false before the first refresh; snapshot is then all zeros
  "snapshot": { ... }     // see below
}
```

`process.stages` ships with every response on purpose, so a new journey step
appears in your dashboard without a code change on your side. **Order charts by
`ordinal`, not by the array order of the breakdowns.**

### snapshot

```jsonc
{
  "schema": "shiftai.telemetry.aggregate",
  "schema_version": "1.0.0",
  "generated_at": "2026-10-05T11:40:53.618570Z",
  "refresh_count": 5,
  "cursor": "...",                       // internal; ignore
  "window": { "first_record_at": "...", "last_record_at": "..." },
  "source": {
    "tenant_id": "levelshift-internal",
    "environment": "dev",
    "process_name": "content-to-campaign",
    "process_version": "1.0.0",
    "sts_schema_version": "2.0.0"
  },
  "totals": { ...metrics..., "cases": 9, "traces": 8 },
  "identity": { "case_ids": [...], "trace_ids": [...] },

  "by_stage":                [ { "stage_id", "stage_label", "stage_ordinal", ...metrics } ],
  "by_agent":                [ { "agent_id", "agent_type", ...metrics } ],
  "by_event_type":           [ { "event_type", ...metrics } ],
  "by_model":                [ { "model", "requested_as", ...metrics } ],
  "by_outcome":              [ { "outcome", ...metrics } ],
  "by_escalation_reason":    [ { "reason", "routed_to", ...metrics } ],
  "by_human_gate_decision":  [ { "decision", "actor_role", ...metrics } ]
}
```

### The metrics block

Every breakdown row and `totals` carry the same fields:

| Field | Meaning |
|---|---|
| `records` | telemetry records in this bucket |
| `llm_calls` | records that invoked a model |
| `input_tokens`, `output_tokens`, `cache_read_tokens` | token usage |
| `cost_usd` | spend, in USD. See the note below |
| `duration_ms_sum`, `duration_ms_count` | for deriving averages across buckets |
| `duration_ms_avg` | `sum / count`, or `null` when nothing was timed |
| `duration_ms_max` | slowest single span |
| `escalations` | records where a case escalated |
| `human_gates` | human approval or return decisions |
| `errors` | error records |

`totals` adds `cases` and `traces`, distinct counts.

**Averages:** `duration_ms_avg` is exact, but if you combine buckets yourself,
divide summed `duration_ms_sum` by summed `duration_ms_count`. Do not average
the averages. There is deliberately no percentile: percentiles cannot be merged
across snapshots exactly, so none is published rather than publishing an
approximation that looks precise.

**Cost:** only incremental per-call spend is summed. The underlying standard
reports a run's cost twice, once per span and once as a run total; the run
total is a restatement and is excluded. `totals.cost_usd` is therefore the real
spend, not double it.

## 4. Real sample

From a live run, abbreviated:

```json
{
  "totals": {
    "records": 64, "llm_calls": 15,
    "input_tokens": 35730, "output_tokens": 12684,
    "cost_usd": 0.064374,
    "duration_ms_avg": 4739.6, "duration_ms_max": 28341,
    "escalations": 2, "human_gates": 3, "errors": 0,
    "cases": 9, "traces": 8
  },
  "by_stage": [
    { "stage_id": "intake",         "stage_label": "Intake",             "stage_ordinal": 1,
      "records": 29, "llm_calls": 3, "cost_usd": 0.001426, "duration_ms_avg": 2108.2,
      "escalations": 2, "human_gates": 3 },
    { "stage_id": "audience_offer", "stage_label": "Audience and offer", "stage_ordinal": 2,
      "records": 1,  "llm_calls": 1, "cost_usd": 0.001173, "duration_ms_avg": 13973.0 },
    { "stage_id": "asset_plan",     "stage_label": "Asset plan",         "stage_ordinal": 3,
      "records": 19, "llm_calls": 3, "cost_usd": 0.010799, "duration_ms_avg": 6936.6 }
  ],
  "by_model": [
    { "model": "gpt-5.4-nano-2026-03-17", "requested_as": "claude-sonnet-5",
      "llm_calls": 15, "cost_usd": 0.064374 }
  ]
}
```

Two things to read carefully in that sample.

**Only the steps that have run appear.** Steps 4 to 9 are absent because no
campaign reached them in that window. Render the full `process.stages` list and
treat a missing breakdown row as zero, rather than assuming the steps do not
exist.

**`model` and `requested_as` can differ.** Here the fleet asked for
`claude-sonnet-5` and Azure served `gpt-5.4-nano`. Cost is priced against the
model that actually answered. If your dashboard shows a model name, show
`model`: that is the one that ran. `requested_as` is the id the spec routes to,
worth surfacing beside it because the gap is real information about what the
deployment is doing. The studio itself now does the same, so the two agree.

## 5. Things to know before you build

**The studio assistant has no stage.** It answers questions across the whole
journey, so attributing its spend to any one step would misreport that step. It
appears in `by_agent` as `studio_assistant` and never in `by_stage`. That means
**`sum(by_stage.cost_usd)` is less than `totals.cost_usd`**, by design. Use
`totals` for the headline number and `by_stage` for the breakdown; do not expect
them to reconcile.

**Snapshots accumulate, they do not replace.** Each refresh aggregates only the
records after the previous cursor and merges them onto the banked figures. This
is because the raw stream is a per-session file that does not survive a restart,
while snapshots live in Postgres. Totals therefore only move forwards.

**`environment` tells you what you are looking at.** `source.environment` is
`dev` today. Do not present dev figures as production.

**CORS.** The bridge allows `http://localhost:5173` by default. A browser-based
dashboard on any other origin will be blocked by the browser until your origin
is added to `BRIDGE_CORS_ORIGINS` on the bridge. Server-side calls are
unaffected. **Tell AiCoE your origin before you start.**

**Check it is live before you start.** `GET /api/telemetry/export` returning
`200` means the hosted bridge has this release; a `404` means it does not yet.
Deployment follows a push to `main`, so there is a window after a release where
the routes exist in the code but not on the server.

## 6. Postman

Import both files from `bridge/postman/`:

- `ShiftAI-C2C-Bridge.postman_collection.json`: five folders, twenty requests,
  every path verified against the live bridge
- `ShiftAI-C2C-Bridge.postman_environment.json`: the variables, with the one
  secret left blank for you to fill

Select the environment, paste `BRIDGE_API_TOKEN` into `token`, and everything
runs. `baseUrl` defaults to the hosted bridge; point it at
`http://localhost:8787` for a local one. `campaignId` and `caseId` come
prefilled with real ids, so most requests work on the first click.

For wiring the AiCoE app itself rather than Postman, the full set of values a
consuming app needs is `bridge/postman/aicoe-integration.env.example`. There
are two of them.

The collection also covers the campaign, draft, review and gate endpoints, in
case the dashboard wants more than the rollup.

## 7. Quick start

```bash
TOKEN=...   # from AiCoE
BASE=https://c2c-agent-bridge.onrender.com

# read
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/telemetry/export" | jq .snapshot.totals

# cost by journey step, in journey order
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/telemetry/export" \
  | jq -r '.snapshot.by_stage | sort_by(.stage_ordinal)
           | .[] | "\(.stage_ordinal). \(.stage_label): $\(.cost_usd)"'

# trigger a recompute (six-hourly)
curl -s -X POST -H "Authorization: Bearer $TOKEN" "$BASE/api/telemetry/export/refresh" \
  | jq '{refreshed, records_added, next_refresh_at}'
```

## 8. Questions

Contact AiCoE. Useful to say which origin you are calling from, whether you
want dev or production figures, and whether six hours is the right refresh
cadence for your dashboard.
