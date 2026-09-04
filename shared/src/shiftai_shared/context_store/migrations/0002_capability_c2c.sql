-- 0002_capability_c2c — Content-to-Campaign Business Capability content:
-- the governance catalog seed (every record kind agents 1-3 write, with
-- classification / personal-data / retention metadata) and the typed read
-- projections. Domain vocabulary is BY DESIGN here (like shared/brand/) —
-- the engine in 0001 stays domain-free.
--
-- Adding a kind (Agents 4-5): new migration inserting its catalog row; CI fails
-- any KIND_* constant in code that has no row here.

BEGIN;

INSERT INTO record_kinds (kind, owner_agent, description, contains_personal_data) VALUES
  -- Agent 1 — Campaign Identification
  ('case',                 'campaign_identification', 'Intake case state machine: request, brief drafts, gap rounds, directives', true),
  ('gap_request',          'campaign_identification', 'Open gap questions routed to the requester', false),
  ('intake_context',       'campaign_identification', 'Normalized request with per-field provenance', true),
  ('approval_task',        'campaign_identification', 'Brief-approval gate routed to the BU Campaign Lead', true),
  ('approved_brief',       'campaign_identification', 'Released campaign brief — the Agent 1 to Agent 2 contract', true),
  ('campaign_calendar',    'campaign_identification', 'Duplicate/timing-conflict reference calendar', false),
  ('failed_request',       'campaign_identification', 'Structured intake failures (never discarded)', false),
  ('human_decision',       'campaign_identification', 'Identity-stamped approve/reject/return decisions', true),
  -- Agent 2 — Campaign-in-a-Box Orchestrator
  ('plan_case',            'campaign_in_a_box',       'Plan state machine: status, versions, confirmations, workspace folder', false),
  ('audience_offer_pack',  'campaign_in_a_box',       'Audience & offer pack with per-claim provenance; deltas are new versions', false),
  ('asset_checklist',      'campaign_in_a_box',       'Reuse/adapt/create checklist with volumes — the fan-out authority', false),
  ('content_outlines',     'campaign_in_a_box',       'Approved content skeletons with planned claims', false),
  ('workflow_plan',        'campaign_in_a_box',       'Back-planned calendar with feasibility and trade-offs', false),
  ('planned_asset',        'campaign_in_a_box',       'Capacity ledger entries (researched-blog months)', false),
  ('registered_asset',     'campaign_in_a_box',       'Content-confirmed assets with confirmation records and claim refs', true),
  ('confirmation',         'campaign_in_a_box',       'Identity-stamped pack/plan/asset confirmations', true),
  ('package_manifest',     'campaign_in_a_box',       'Hashed final package with claim-lineage index — the Agent 5 input', false),
  ('completeness_report',  'campaign_in_a_box',       'Blocking packaging diffs, never padded or trimmed', false),
  ('failed_plan_run',      'campaign_in_a_box',       'Structured planning/packaging failures', false),
  -- Agent 3 — Content Repurposing
  ('repurpose_case',       'content_repurposing',     'Flagship-first state machine with the flagship confirmation record', true),
  ('staged_draft',         'content_repurposing',     'Versioned drafts with claim markers, lineage and self-check results', true),
  ('claim_inventory',      'content_repurposing',     'Verbatim-verified claims extracted from the confirmed flagship', false),
  ('content_gap_note',     'content_repurposing',     'Evidence the agent needed but refused to invent', false),
  ('failed_repurpose_run', 'content_repurposing',     'Structured drafting/fan-out failures', false)
ON CONFLICT (kind) DO NOTHING;

-- ------------------------------------------------------------- typed views
-- Derived read model over the jsonb store: one write path, zero drift.
-- security_invoker = true so row-level security ALWAYS applies to the reader.

CREATE OR REPLACE VIEW v_plan_cases WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.key                                   AS campaign_id,
       r.value ->> 'status'                    AS status,
       (r.value ->> 'pack_version')::int       AS pack_version,
       (r.value ->> 'plan_version')::int       AS plan_version,
       (r.value ->> 'manifest_version')::int   AS manifest_version,
       r.value ->> 'folder'                    AS folder,
       r.value ->> 'campaign_slug'             AS campaign_slug,
       r.value ->> 'trace_id'                  AS trace_id,
       r.version, r.created_at
FROM context_records r
JOIN (SELECT tenant_id, key, max(version) AS mv FROM context_records
      WHERE kind = 'plan_case' GROUP BY tenant_id, key) latest
  ON r.tenant_id = latest.tenant_id AND r.key = latest.key AND r.version = latest.mv
WHERE r.kind = 'plan_case';

CREATE OR REPLACE VIEW v_approved_briefs WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.key                                   AS campaign_id,
       r.value -> 'brief' ->> 'case_id'        AS case_id,
       r.value -> 'brief' ->> 'status'         AS status,
       r.value ->> 'doc_ref'                   AS doc_ref,
       r.value ->> 'released_at'               AS released_at,
       r.version, r.created_at
FROM context_records r
JOIN (SELECT tenant_id, key, max(version) AS mv FROM context_records
      WHERE kind = 'approved_brief' GROUP BY tenant_id, key) latest
  ON r.tenant_id = latest.tenant_id AND r.key = latest.key AND r.version = latest.mv
WHERE r.kind = 'approved_brief';

CREATE OR REPLACE VIEW v_registered_assets WITH (security_invoker = true) AS
SELECT r.tenant_id,
       split_part(r.key, ':', 1)               AS campaign_id,
       r.value ->> 'asset_id'                  AS asset_id,
       r.value ->> 'asset_type'                AS asset_type,
       (r.value ->> 'version')::int            AS asset_version,
       r.value ->> 'status'                    AS status,
       r.value ->> 'filename'                  AS filename,
       r.value -> 'claim_refs'                 AS claim_refs,
       r.value -> 'confirmation' ->> 'actor_id' AS confirmed_by,
       r.version, r.created_at
FROM context_records r
JOIN (SELECT tenant_id, key, max(version) AS mv FROM context_records
      WHERE kind = 'registered_asset' GROUP BY tenant_id, key) latest
  ON r.tenant_id = latest.tenant_id AND r.key = latest.key AND r.version = latest.mv
WHERE r.kind = 'registered_asset';

CREATE OR REPLACE VIEW v_staged_drafts WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.value ->> 'campaign_id'               AS campaign_id,
       r.value ->> 'asset_id'                  AS asset_id,
       r.value ->> 'asset_type'                AS asset_type,
       r.value ->> 'kind'                      AS draft_kind,
       (r.value ->> 'version')::int            AS draft_version,
       r.value ->> 'status'                    AS status,
       (r.value -> 'self_check' ->> 'passed')::boolean AS self_check_passed,
       r.value -> 'claim_lineage'              AS claim_lineage,
       r.created_at
FROM context_records r
WHERE r.kind = 'staged_draft';

CREATE OR REPLACE VIEW v_claim_inventories WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.value ->> 'campaign_id'               AS campaign_id,
       (r.value ->> 'flagship_version')::int   AS flagship_version,
       r.value ->> 'method'                    AS method,
       jsonb_array_length(r.value -> 'items')  AS item_count,
       (r.value ->> 'dropped_unverified')::int AS dropped_unverified,
       r.created_at
FROM context_records r
WHERE r.kind = 'claim_inventory';

CREATE OR REPLACE VIEW v_package_manifests WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.key                                   AS campaign_id,
       r.value ->> 'manifest_id'               AS manifest_id,
       (r.value ->> 'version')::int            AS manifest_version,
       r.value ->> 'status'                    AS status,
       jsonb_array_length(r.value -> 'assets') AS asset_count,
       r.value -> 'claim_lineage_index'        AS claim_lineage_index,
       r.version, r.created_at
FROM context_records r
JOIN (SELECT tenant_id, key, max(version) AS mv FROM context_records
      WHERE kind = 'package_manifest' GROUP BY tenant_id, key) latest
  ON r.tenant_id = latest.tenant_id AND r.key = latest.key AND r.version = latest.mv
WHERE r.kind = 'package_manifest';

CREATE OR REPLACE VIEW v_gap_notes WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.value ->> 'campaign_id'               AS campaign_id,
       r.value ->> 'asset_id'                  AS asset_id,
       r.value ->> 'section'                   AS section,
       r.value ->> 'needed'                    AS needed,
       r.created_at
FROM context_records r
WHERE r.kind = 'content_gap_note';

CREATE OR REPLACE VIEW v_failed_runs WITH (security_invoker = true) AS
SELECT r.tenant_id,
       r.kind,
       r.value ->> 'campaign_id'               AS campaign_id,
       r.value ->> 'error_type'                AS error_type,
       r.value ->> 'detail'                    AS detail,
       r.created_at
FROM context_records r
WHERE r.kind IN ('failed_request', 'failed_plan_run', 'failed_repurpose_run');

-- The audit crown jewel: every identity-stamped human decision, one view.
CREATE OR REPLACE VIEW v_hitl_decisions WITH (security_invoker = true) AS
SELECT r.tenant_id,
       'confirmation'                          AS source,
       r.value ->> 'campaign_id'               AS campaign_id,
       r.value ->> 'kind'                      AS gate,
       r.value ->> 'decision'                  AS decision,
       r.value ->> 'actor_id'                  AS actor_id,
       r.value ->> 'actor_role'                AS actor_role,
       r.value ->> 'timestamp'                 AS decided_at,
       r.created_at
FROM context_records r
WHERE r.kind = 'confirmation'
UNION ALL
SELECT r.tenant_id,
       'human_decision',
       r.value ->> 'campaign_id',
       'brief_approval',
       r.value ->> 'decision',
       r.value ->> 'actor_id',
       r.value ->> 'actor_role',
       r.value ->> 'timestamp',
       r.created_at
FROM context_records r
WHERE r.kind = 'human_decision'
UNION ALL
SELECT r.tenant_id,
       'flagship_confirmation',
       r.key,
       'flagship_content',
       r.value -> 'flagship_confirmation' ->> 'decision',
       r.value -> 'flagship_confirmation' ->> 'actor_id',
       r.value -> 'flagship_confirmation' ->> 'actor_role',
       r.value -> 'flagship_confirmation' ->> 'timestamp',
       r.created_at
FROM context_records r
JOIN (SELECT tenant_id, key, max(version) AS mv FROM context_records
      WHERE kind = 'repurpose_case' GROUP BY tenant_id, key) latest
  ON r.tenant_id = latest.tenant_id AND r.key = latest.key AND r.version = latest.mv
WHERE r.kind = 'repurpose_case' AND r.value ? 'flagship_confirmation'
  AND r.value -> 'flagship_confirmation' IS NOT NULL;

GRANT SELECT ON v_plan_cases, v_approved_briefs, v_registered_assets, v_staged_drafts,
  v_claim_inventories, v_package_manifests, v_gap_notes, v_failed_runs, v_hitl_decisions
  TO c2c_agent, c2c_readonly;

COMMIT;
