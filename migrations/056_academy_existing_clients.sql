-- Phones of clients who were already talking to the school on WhatsApp before
-- the bot existed. Academy bots stay silent for these numbers — the bot has no
-- record of their earlier conversation, so a manager handles them instead.
-- The arena bot ignores this table. Loaded by scripts/import_existing_clients.py.
CREATE TABLE IF NOT EXISTS academy_existing_clients (
    phone       VARCHAR(20) PRIMARY KEY,   -- digits only, e.g. 77001234567
    source      VARCHAR(32) NOT NULL DEFAULT 'import',
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
