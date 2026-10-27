-- academy_trials.signup_for: who the signup is for, as the bot read the chat.
-- 'self' — the client signs up themselves (a teenager in the children's group
-- too), 'other' — a child or another person. NULL means "not clear yet".

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS signup_for TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'academy_trials_signup_for_check') THEN
        ALTER TABLE academy_trials
            ADD CONSTRAINT academy_trials_signup_for_check CHECK (signup_for IS NULL OR signup_for IN ('self', 'other'));
    END IF;
END $$;
