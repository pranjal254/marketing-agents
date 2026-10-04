-- 0007_capability_c2c_assistant — governance catalog row for the studio
-- assistant's stored conversations. Capability content, like 0002 to 0006; the
-- engine (0001) is untouched.
--
-- A conversation holds questions people typed and the answers they were given,
-- stamped with who asked. That is personal data on both counts: the identity
-- and the content of the question itself. Retained for two years in line with
-- the other operational records, not the seven-year approval chain, because a
-- chat is not a decision record.
--
-- Separate migration rather than an edit to 0006: applied migrations are
-- recorded by filename and never re-run, so changing a file that has already
-- landed somewhere would silently skip the change there.

BEGIN;

INSERT INTO record_kinds (kind, owner_agent, description, contains_personal_data, retention_days) VALUES
  ('assistant_conversation', 'studio_assistant', 'Studio assistant chat transcript: questions, answers, tool calls and the actor who asked', true, 730)
ON CONFLICT (kind) DO NOTHING;

COMMIT;
