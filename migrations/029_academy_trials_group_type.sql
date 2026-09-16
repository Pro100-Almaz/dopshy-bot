-- Scope a trial row to a curriculum from the moment it is created.
--
-- The deterministic flow scopes drafts through trial_sessions.chat_id, which is
-- "{phone_number_id}:{sender}" and therefore already per-bot. The LLM flow drops
-- the session row and identifies a draft by phone + state='draft' alone, so the
-- per-bot scoping has to live on the trial itself. group_id cannot carry it: it
-- is still NULL until the parent has picked a date and time.

ALTER TABLE academy_trials ADD COLUMN IF NOT EXISTS group_type VARCHAR(20);

UPDATE academy_trials t
SET group_type = g.group_type
FROM academy_groups g
WHERE t.group_id = g.id AND t.group_type IS NULL;

-- The LLM flow looks a draft up on every incoming message.
CREATE INDEX IF NOT EXISTS idx_academy_trials_phone_state
    ON academy_trials (phone, state);
