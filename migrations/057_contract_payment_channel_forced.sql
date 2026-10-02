-- The payment channel of a contract installment is now decided when it is SENT
-- (Kaspi invoice if ApiPay is configured and the number is in Kaspi, otherwise
-- a WhatsApp link), not frozen when the contract is created.
--
-- The one thing that must still be remembered from creation is a manager's
-- explicit choice of the WhatsApp link ("payment_plan.channel":
-- "whatsapp_link"): such a contract keeps getting links even once the number
-- is in Kaspi. contracts.payment_channel stays, now meaning "the channel the
-- last installment went out on".

ALTER TABLE contracts ADD COLUMN IF NOT EXISTS payment_channel_forced BOOLEAN NOT NULL DEFAULT FALSE;
