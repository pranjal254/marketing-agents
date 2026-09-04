-- 0001_engine — domain-free storage engine for the shared Context Store.
-- Layering mirrors shared/: this file is control plane / engine mechanics only;
-- Business Capability content (kind catalog seed, typed views) lives in 0002.
--
-- Guarantees enforced HERE, not by convention:
--   * versioned append-only: no UPDATE/DELETE exists for any role, and a trigger
--     raises even if grants are misconfigured;
--   * tenancy: row-level security on the app.tenant_id connection setting;
--   * least privilege: c2c_agent may INSERT+SELECT, c2c_readonly may SELECT.

BEGIN;

-- ---------------------------------------------------------------- roles
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'c2c_agent') THEN
    CREATE ROLE c2c_agent NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'c2c_readonly') THEN
    CREATE ROLE c2c_readonly NOLOGIN;
  END IF;
END
$$;

-- ------------------------------------------------------- governance catalog
-- Every record kind is registered here with its governance metadata. Enforced in
-- CI (a KIND_* constant without a catalog row fails the build), NOT by a runtime
-- FK — dev (SQLite) and prod (Postgres) must behave identically at runtime.
CREATE TABLE IF NOT EXISTS record_kinds (
  kind                   text PRIMARY KEY,
  owner_agent            text NOT NULL,
  description            text NOT NULL,
  data_classification    text NOT NULL DEFAULT 'confidential',
  contains_personal_data boolean NOT NULL DEFAULT false,
  retention_days         integer NOT NULL DEFAULT 0, -- 0 = retain until policy set
  registered_at          timestamptz NOT NULL DEFAULT now()
);
COMMENT ON TABLE record_kinds IS
  'Governance catalog: classification, personal-data flag and retention metadata per record kind. Retention is metadata for a future purge job — no deletion code exists.';

-- ------------------------------------------------------------ context records
-- The store protocol verbatim: put() inserts version N+1 for (tenant, kind, key);
-- get() reads the max version. History is the table itself.
CREATE TABLE IF NOT EXISTS context_records (
  tenant_id    text        NOT NULL,
  kind         text        NOT NULL,
  key          text        NOT NULL,
  version      integer     NOT NULL CHECK (version >= 1),
  value        jsonb       NOT NULL,
  value_sha256 char(64)    NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  created_by   text        NOT NULL DEFAULT coalesce(current_setting('app.agent_id', true), 'unknown'),
  PRIMARY KEY (tenant_id, kind, key, version)
);
COMMENT ON TABLE context_records IS
  'Versioned append-only shared state. Every put() is a new version; nothing is ever updated or deleted. value_sha256 = sha256 of the canonical (sorted-keys) JSON.';
CREATE INDEX IF NOT EXISTS context_records_kind_idx
  ON context_records (tenant_id, kind, created_at DESC);

-- --------------------------------------------------------- idempotency ledger
CREATE TABLE IF NOT EXISTS idempotency_keys (
  tenant_id  text        NOT NULL,
  key        text        NOT NULL,
  result     jsonb       NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, key)
);
COMMENT ON TABLE idempotency_keys IS
  'Side-effect ledger: a key is written once, before-check prevents duplicate external actions. Insert-only.';

-- ------------------------------------------------------------ telemetry events
-- Ready for the streaming sink (Phase B). Generic envelope columns are promoted
-- for indexing; the full schema-validated record rides in `record`.
CREATE TABLE IF NOT EXISTS telemetry_events (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id  text        NOT NULL,
  agent_id   text        NOT NULL,
  event_type text        NOT NULL,
  case_id    text        NOT NULL,
  trace_id   text        NOT NULL,
  run_id     text,
  span_id    text,
  ts         timestamptz NOT NULL DEFAULT now(),
  record     jsonb       NOT NULL
);
CREATE INDEX IF NOT EXISTS telemetry_trace_idx ON telemetry_events (tenant_id, trace_id, id);
CREATE INDEX IF NOT EXISTS telemetry_case_idx  ON telemetry_events (tenant_id, case_id, id);
CREATE INDEX IF NOT EXISTS telemetry_type_idx  ON telemetry_events (tenant_id, agent_id, event_type, ts);

-- --------------------------------------------------- append-only enforcement
CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only store: % on % is forbidden', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS context_records_append_only ON context_records;
CREATE TRIGGER context_records_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON context_records
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS idempotency_keys_append_only ON idempotency_keys;
CREATE TRIGGER idempotency_keys_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON idempotency_keys
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

DROP TRIGGER IF EXISTS telemetry_events_append_only ON telemetry_events;
CREATE TRIGGER telemetry_events_append_only
  BEFORE UPDATE OR DELETE OR TRUNCATE ON telemetry_events
  FOR EACH STATEMENT EXECUTE FUNCTION forbid_mutation();

-- ------------------------------------------------------- row-level security
-- FORCE: tenancy applies even to the table owner (single-user DSNs on managed
-- Postgres connect as the owner — isolation must not depend on role hygiene).
ALTER TABLE context_records  ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_records  FORCE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_keys FORCE ROW LEVEL SECURITY;
ALTER TABLE telemetry_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE telemetry_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON context_records;
CREATE POLICY tenant_isolation ON context_records
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS tenant_isolation ON idempotency_keys;
CREATE POLICY tenant_isolation ON idempotency_keys
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

DROP POLICY IF EXISTS tenant_isolation ON telemetry_events;
CREATE POLICY tenant_isolation ON telemetry_events
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- ------------------------------------------------------------------- grants
GRANT SELECT, INSERT ON context_records, idempotency_keys, telemetry_events TO c2c_agent;
GRANT SELECT ON record_kinds TO c2c_agent;
GRANT SELECT ON context_records, idempotency_keys, telemetry_events, record_kinds TO c2c_readonly;
REVOKE UPDATE, DELETE, TRUNCATE ON context_records, idempotency_keys, telemetry_events
  FROM c2c_agent, c2c_readonly;

COMMIT;
