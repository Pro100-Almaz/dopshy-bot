ALTER TABLE academy_trials ADD COLUMN IF NOT EXISTS client_token   UUID NOT NULL DEFAULT gen_random_uuid();
