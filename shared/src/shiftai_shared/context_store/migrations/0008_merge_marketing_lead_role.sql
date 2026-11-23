-- 0008_merge_marketing_lead_role — one lead role.
--
-- Post-feedback: the Marketing Lead and BU Campaign Lead roles are merged into a
-- single "BU Campaign Lead" role that carries every permission of both
-- (including requesting campaigns). Existing directory rows are folded in; the
-- studio also normalises the old label on read, so historic records stay valid.

BEGIN;

UPDATE users
   SET role = 'BU Campaign Lead', updated_at = now()
 WHERE role = 'Marketing Lead';

COMMIT;
