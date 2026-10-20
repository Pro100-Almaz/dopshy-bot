-- Academy groups are split by audience: children's groups and the adult group.
--
-- academy_groups.audience: who the group is for. Every existing group is a
-- children's group, hence the default.
--
-- academy_trials.audience: the group the client picked before the signup
-- questions start. NULL means "not chosen yet" — the bot asks first.

ALTER TABLE academy_groups
    ADD COLUMN IF NOT EXISTS audience TEXT NOT NULL DEFAULT 'child';

ALTER TABLE academy_trials
    ADD COLUMN IF NOT EXISTS audience TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'academy_groups_audience_check') THEN
        ALTER TABLE academy_groups
            ADD CONSTRAINT academy_groups_audience_check CHECK (audience IN ('child', 'adult'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'academy_trials_audience_check') THEN
        ALTER TABLE academy_trials
            ADD CONSTRAINT academy_trials_audience_check CHECK (audience IS NULL OR audience IN ('child', 'adult'));
    END IF;
END $$;
