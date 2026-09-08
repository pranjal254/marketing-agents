-- 0004_capability_c2c_agent5 — governance catalog rows for the record kinds the
-- Quality Gate & Approval Agent (Agent 5) writes. Capability content, like
-- 0002/0003; the engine (0001) is untouched. Approval records and calibration
-- logs carry reviewer identities and judgments — internal audit data, flagged
-- as personal data; approval chains retain per record-keeping policy (7 years).

BEGIN;

INSERT INTO record_kinds (kind, owner_agent, description, contains_personal_data, retention_days) VALUES
  ('compliance_report',    'quality_gate_approval', 'Per-asset gate findings: rule id, severity, location, quote, verdict', false, 730),
  ('package_gate_state',   'quality_gate_approval', 'Package state machine through gate, review, lock and release', false, 730),
  ('approval_review_task', 'quality_gate_approval', 'Sequenced human review assignments with SLA, reminders, decisions', true, 2555),
  ('approval_record',      'quality_gate_approval', 'Identity/timestamp/version/hash record of every approval or return', true, 2555),
  ('calibration_event',    'quality_gate_approval', 'False-positive/false-negative labeled examples for rules tuning (reviewer judgments, internal only)', true, 730)
ON CONFLICT (kind) DO NOTHING;

COMMIT;
