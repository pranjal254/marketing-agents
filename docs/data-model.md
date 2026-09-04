# Data Model & Storage Architecture — Content to Campaign

**Status: v1.0, 2026-09-03.** Supersedes the "defer the database" decision in
`PLAN-content-repurposing.md` §0 — the model and the Postgres binding are built
now; *enabling* it stays an env-var decision (`DATABASE_URL`), zero agent code
change either way.

## 1. The three planes of data

Every byte this system produces lives in exactly one of three planes, each with
its own system of record, mutation rules, and access model:

| Plane | System of record | Mutability | Contents |
|---|---|---|---|
| **Documents** | OneDrive/SharePoint (dev: local workspace mirror) | **Additive-only** — the workspace protocol has no delete/move/overwrite | Word drafts, packs, trackers, claim maps, final snapshots |
| **State & metadata** | **Context Store** (dev: SQLite · staging/prod: Postgres) | **Versioned append-only** — `put` inserts version N+1, nothing is ever updated or deleted | Cases, briefs, packs, checklists, drafts registry, claim lineage, HITL records, manifests, gap notes, failures |
| **Telemetry** | STS v2 JSONL stream (staging/prod: `telemetry_events` table + Execution Studio) | **Append-only** | Every schema-validated STS record: decisions, gates, escalations, costs |

The join between planes is by **reference + hash**: a state record points at a
document by ref and carries its `sha256`; a document changed outside the recorded
flow is *detected* (Agent 2's hash-mismatch halt), never silently accepted. This
is the standard blob-store + metadata-catalog pattern; the catalog is authority
on *what is true*, the blob store on *what the bytes are*, telemetry on *what
happened*.

## 2. Why the Context Store stays a versioned KV core (and not 24 tables)

The agents write through one protocol: `put/get/get_all_versions/query` over
`(kind, key) → value` with server-side versioning. That protocol **is** the data
model's write path, and we keep it — deliberately:

1. **One write path, zero drift.** Typed relational projections are *derived*
   (SQL views over `jsonb`), never dual-written. A projection can be wrong only
   if the view is wrong — not because two writers raced.
2. **Full temporal history for free.** Version N is never destroyed; every
   entity's history is `get_all_versions`. Deltas-as-new-versions (pack v2 after
   a Marketing Lead delta) and rework-as-new-versions (draft v2) already rely on
   this.
3. **Schema evolution without migrations on the hot path.** Agents 4–5 add new
   record kinds by writing them; governance catches up through the **kind
   catalog** (below), enforced in CI, not by a runtime FK that would behave
   differently in dev (SQLite) and prod (Postgres).
4. **Protocol portability.** Execution Studio's production store binds the same
   protocol at onboarding — the model survives the platform swap.

What we add on top of the KV core is what enterprise-grade requires: tenancy,
row-level security, integrity hashes, an enforced append-only surface, a
governance catalog with classification + retention, and typed views for
querying, BI, and audit.

## 3. Entity catalog (the 24 record kinds)

Key format is part of the contract. `cid` = campaign_id.

### Agent 1 — Campaign Identification (`campaign_identification`)
| kind | key | contents | consumers |
|---|---|---|---|
| `case` | case_id | intake case state machine (status, request, brief, gap rounds, directives) | bridge/studio, Agent 2 (trace) |
| `gap_request` | case_id | open gap questions per round | studio |
| `intake_context` | case_id | normalized request + derivations, provenance | audit |
| `approval_task` | case_id | routed BU-lead gate | studio |
| `approved_brief` | **cid** | the released brief (fields + provenance + classification) — **the Agent 1→2 contract** | Agent 2 |
| `campaign_calendar` | tenant scope | duplicate/conflict reference | Agent 1 |
| `failed_request` | case_id:ts | structured intake failures | ops |
| `human_decision` | case_id:ts | identity-stamped approve/reject/return | audit |

### Agent 2 — Campaign-in-a-Box (`campaign_in_a_box`)
| kind | key | contents | consumers |
|---|---|---|---|
| `plan_case` | cid | plan state machine (status, versions, confirmations, folder, slug, trace) | Agent 3, bridge |
| `audience_offer_pack` | cid | personas, proof points (w/ per-claim provenance), angles — versioned by delta | Agent 3 |
| `asset_checklist` | cid | reuse/adapt/create decisions + volumes — **the fan-out authority** | Agent 3, packaging |
| `content_outlines` | cid | approved skeletons w/ planned claims | Agent 3 |
| `workflow_plan` | cid | back-planned calendar, feasibility, trade-offs | studio |
| `planned_asset` | cid:asset | capacity ledger (researched-blog months) | Agent 2 fleet-wide |
| `registered_asset` | cid:asset | content-confirmed asset + confirmation record + claim refs | packaging |
| `confirmation` | cid:kind:ts | identity-stamped pack/plan/asset confirmations | audit |
| `package_manifest` | cid | hashed final package + claim-lineage index — **the Agent 5 input** | Agent 5 |
| `completeness_report` | cid | blocking diffs, never padded | studio |
| `failed_plan_run` | cid:ts | structured failures | ops |

### Agent 3 — Content Repurposing (`content_repurposing`)
| kind | key | contents | consumers |
|---|---|---|---|
| `repurpose_case` | cid | flagship-first state machine + flagship confirmation record | Agent 4, bridge |
| `staged_draft` | cid:asset:vN | versioned draft + markers + lineage + self-check (one record per version — additive) | Agent 4, packaging |
| `claim_inventory` | cid:vN | verbatim-verified claims from the confirmed flagship | Agent 3 fan-out |
| `content_gap_note` | cid:gap_id | evidence the agent refused to invent | humans |
| `failed_repurpose_run` | cid:ts | structured failures | ops |

Agents 4–5 will add kinds (e.g. `review_round`, `revision_set`,
`compliance_report`, `signoff_record`); each lands with a catalog row in a new
migration — CI fails if a `KIND_*` constant exists in code without one.

## 4. Governance model (in the schema, not in a wiki)

- **Kind catalog** (`record_kinds`): every kind carries `owner_agent`,
  `description`, `data_classification` (all `confidential` in Phase 1 — no
  customer PII by spec), `contains_personal_data` (true where actor emails
  appear: cases, confirmations, decisions, drafts — GDPR locate-and-explain
  path), `retention_days` (0 = retain; a future purge job reads this column —
  purge policy is a governance decision, deliberately NOT implemented as code
  yet).
- **Tenancy + RLS**: every table carries `tenant_id`; row-level security
  filters on the `app.tenant_id` connection setting. One database can host
  multiple tenants without a code branch, and a leaked read-only credential
  still sees only its tenant.
- **Append-only, enforced three ways**: the adapter has no UPDATE/DELETE SQL;
  role grants exclude UPDATE/DELETE/TRUNCATE; and a trigger raises on any
  mutation attempt regardless of role. History is not a backup — it *is* the
  table.
- **Integrity**: each record stores `value_sha256` over canonical JSON. Document
  hashes (manifests) already exist; this extends tamper-evidence to state.
- **Identity**: `created_by` records the writing service (`app.agent_id`
  setting); human identity lives *inside* the records (HITL discipline), and
  `v_hitl_decisions` projects every human decision — actor, role, decision,
  timestamp — as the single audit view.
- **Least privilege roles**: `c2c_agent` (INSERT+SELECT only),
  `c2c_readonly` (SELECT only — dashboards/BI), migrations run as owner. No
  role can mutate or delete.
- **Secrets**: `DATABASE_URL` env-only (Key Vault in Azure); TLS
  (`sslmode=require`) is the documented default for hosted databases.

## 5. Typed projections (read model)

Views in `0002_capability_c2c.sql` — derived from `jsonb`, security-invoker so
RLS always applies: `v_plan_cases`, `v_approved_briefs`, `v_registered_assets`,
`v_staged_drafts`, `v_claim_inventories`, `v_package_manifests`, `v_gap_notes`,
`v_failed_runs`, and `v_hitl_decisions` (union of every human gate record).
These are the BI/audit surface; the studio keeps using the bridge API.

Layering mirrors `shared/`: **0001 is engine** (domain-free tables, RLS, roles,
triggers), **0002 is Business Capability content** (the C2C catalog + views),
exactly like `brand/` vs `control_plane.py`.

## 6. Bindings per environment

| | dev (today) | staging (Render/Neon) | production (Azure) |
|---|---|---|---|
| Context Store | SQLite per bridge session | **Postgres** (`DATABASE_URL`) — state survives restarts/redeploys | Azure Database for PostgreSQL + Key Vault + managed identity |
| Documents | local workspace mirror | local (ephemeral) | OneDrive/SharePoint via Graph |
| Telemetry | JSONL + SSE | JSONL (+ `telemetry_events` ready) | Execution Studio + `telemetry_events` |
| Idempotency | SQLite | **Postgres** (same DB) | Postgres |

**Honest caveat for staging**: with Postgres enabled on Render, *metadata*
survives a restart but the *workspace files* do not (free-tier ephemeral disk).
The system detects the divergence by design — snapshot reads fail or hashes
mismatch and the case escalates rather than lying. Full durability arrives when
the OneDrive binding lands; that pairing is why documents and state were split
into separate planes in the first place.

## 7. Decisions & rejected alternatives

| Decision | Rejected alternative | Why |
|---|---|---|
| Versioned KV core + derived views | 24 normalized tables written by agents | dual-write drift, migration on every agent change, breaks the store protocol contract |
| Kind catalog enforced in **CI** | runtime FK on `kind` | dev(SQLite)/prod(Postgres) behavior divergence; a prod-only hard failure is the worst failure mode |
| RLS on `app.tenant_id` setting | tenant per schema/database | operational sprawl; RLS is the PG-native, auditable mechanism |
| Views `security_invoker = true` | owner-rights views | owner-rights would bypass RLS; invoker keeps tenancy airtight |
| Retention as catalog **metadata** | TTL deletion job now | deletion policy needs a governance sign-off; the column makes it a config change later, not a redesign |
| psycopg3 as an **optional extra** | hard dependency | agents never import it; only deployments that set `DATABASE_URL` need it |

## 8. How to enable

```
pip install -e "./shared[postgres]"
# .env / Render / Azure app settings:
DATABASE_URL=postgresql://user:pass@host:5432/c2c?sslmode=require
python -m shiftai_shared.context_store.migrate     # applies migrations, idempotent
```
The bridge selects Postgres automatically when `DATABASE_URL` is set (health
endpoint reports `"store": "postgres"`); without it, SQLite as today. Free-tier
option for staging: Neon (serverless Postgres) — no code change.
