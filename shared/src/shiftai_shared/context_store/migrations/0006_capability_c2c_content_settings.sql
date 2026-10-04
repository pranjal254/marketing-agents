-- 0006_capability_c2c_content_settings — governance catalog rows for the two
-- record kinds added for the Marketing content-controls and telemetry-export
-- work. Capability content, like 0002 to 0005; the engine (0001) is untouched.
--
-- content_settings carries the id and role of the Content Writer who chose how
-- many of each asset to produce and how long each should be, so it is flagged
-- as personal data. It is kept as long as the approval chain it explains,
-- because "why did this campaign ship five emails" is an audit question.
--
-- telemetry_snapshot holds only aggregate counters and the case and trace ids
-- they were computed from. No person appears in it, so it is not personal data;
-- it is retained for two years, in line with the other operational records.

BEGIN;

INSERT INTO record_kinds (kind, owner_agent, description, contains_personal_data, retention_days) VALUES
  ('content_settings',   'content_repurposing', 'Per-campaign variant counts and word ranges per asset, identity-stamped and versioned', true, 2555),
  ('telemetry_snapshot', 'content_repurposing', 'Banked aggregate rollup of the STS stream served to the execution studio', false, 730)
ON CONFLICT (kind) DO NOTHING;

COMMIT;
