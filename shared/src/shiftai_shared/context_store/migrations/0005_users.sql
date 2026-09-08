-- 0005_users — the workspace user directory (operational, mutable).
--
-- Unlike the append-only capability records, this is a plain directory table:
-- the admin Users page creates, edits and removes people, and it must persist
-- across deployments and browsers. Auth stays a shared workspace password
-- (known to admins), so NO password or secret is stored here — only identity,
-- role and status. The five default users are seeded idempotently.

BEGIN;

CREATE TABLE IF NOT EXISTS users (
  id          text PRIMARY KEY,
  name        text NOT NULL,
  email       text NOT NULL UNIQUE,
  role        text NOT NULL,
  status      text NOT NULL DEFAULT 'Active',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT, UPDATE, DELETE ON users TO c2c_agent;
GRANT SELECT ON users TO c2c_readonly;

-- Default workspace users (ids match the studio's task routing; do not change).
INSERT INTO users (id, name, email, role, status) VALUES
  ('aicoe',  'AiCoE Admin',            'aicoe@levelshift.com',    'AiCoE Admin',                 'Active'),
  ('marcus', 'Ramya Srinivasan',       'ramya_s4@levelshift.com', 'BU Campaign Lead',            'Active'),
  ('rishi',  'Neeraj Vasant Sangani',  'neeraj_v@levelshift.com', 'Marketing Lead',              'Active'),
  ('jen',    'Jen Cook',               'jen.cook@levelshift.com', 'Content Writer',              'Active'),
  ('tom',    'Tom Smith',              'tom.smith@levelshift.com','Grammar / Quality Reviewer',  'Active')
ON CONFLICT (id) DO NOTHING;

COMMIT;
