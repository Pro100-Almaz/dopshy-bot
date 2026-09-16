ALTER TABLE ycloud_enabled
    ADD COLUMN IF NOT EXISTS bot_name VARCHAR(40);

UPDATE ycloud_enabled
SET bot_name = 'arena'
WHERE bot_name IS NULL;

ALTER TABLE ycloud_enabled
    ALTER COLUMN bot_name SET NOT NULL;

ALTER TABLE ycloud_enabled
    DROP CONSTRAINT IF EXISTS single_settings_row;

ALTER TABLE ycloud_enabled
    ADD CONSTRAINT ycloud_enabled_bot_name_check
    CHECK (bot_name IN ('arena', 'football_academy', 'boxing_academy'));

CREATE UNIQUE INDEX IF NOT EXISTS ycloud_enabled_bot_name_key
    ON ycloud_enabled (bot_name);
