-- 0003_capability_c2c_agent4 — governance catalog rows for the record kinds the
-- Content Collaboration & Iteration Agent (Agent 4) writes. Capability content,
-- like 0002; the engine (0001) is untouched. Reviewer commentary is
-- internal-only (spec Data Sensitivity) — flagged as personal data.

BEGIN;

INSERT INTO record_kinds (kind, owner_agent, description, contains_personal_data) VALUES
  ('review_assignment', 'collaboration_iteration', 'Per-asset review state machine: reviewers, due date, status, round count', true),
  ('feedback_item',     'collaboration_iteration', 'One reviewer comment with attribution and section anchor (internal-only)', true),
  ('review_round',      'collaboration_iteration', 'One consolidation+revision round: classifications, applications, edit summary', true),
  ('conflict_record',   'collaboration_iteration', 'Contradictory reviewer positions, quoted, held for the Marketing Lead', true),
  ('iteration_metrics', 'collaboration_iteration', 'Rounds, feedback volume, time-in-review per asset (sub-process 5 raw material)', false)
ON CONFLICT (kind) DO NOTHING;

COMMIT;
