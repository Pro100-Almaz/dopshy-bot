CREATE TABLE IF NOT EXISTS ycloud_enabled (
    id          SMALLINT     PRIMARY KEY DEFAULT 1,
    is_enabled  BOOLEAN      NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by  VARCHAR(100),

    CONSTRAINT single_settings_row CHECK (id = 1)
);

ALTER TABLE ycloud_enabled ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE ycloud_enabled ADD COLUMN IF NOT EXISTS updated_by VARCHAR(100);

INSERT INTO ycloud_enabled DEFAULT VALUES
ON CONFLICT (id) DO NOTHING;
