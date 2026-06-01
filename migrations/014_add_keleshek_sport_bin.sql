-- Add the ТОО "КЕЛЕШЕК СПОРТ" / ФШ Допшы fiscal-receipt seller BIN as a second
-- active Kaspi recipient. Fiscal receipts carry this BIN; the legacy P2P-style
-- DOPSHY recipient stays valid alongside it.

INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'kaspi', '250740003149', 'ТОО КЕЛЕШЕК СПОРТ', NULL
WHERE NOT EXISTS (
    SELECT 1 FROM payment_recipients WHERE bank = 'kaspi' AND bin = '250740003149'
);
