-- Per-contact bot pause switch.
-- When a phone number has a row here with paused = TRUE, the bot stays silent
-- for that customer so a human manager can take over the conversation.
-- reason: 'manual' (toggled from the manager UI) or 'auto' (a human agent
-- replied on WhatsApp, detected via the whatsapp.smb.message.echoes webhook).
CREATE TABLE IF NOT EXISTS bot_paused_contacts (
    phone       VARCHAR(20) PRIMARY KEY,
    paused      BOOLEAN     NOT NULL DEFAULT TRUE,
    reason      VARCHAR(16) NOT NULL DEFAULT 'manual',
    paused_by   VARCHAR(100),
    paused_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
