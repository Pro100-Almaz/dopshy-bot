INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'kaspi', '870203301478', 'DOPSHY', NULL
WHERE NOT EXISTS (SELECT 1 FROM payment_recipients WHERE bank = 'kaspi' AND bin = '870203301478');

INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'halyk', NULL, 'Мухтар', '77029721819'
WHERE NOT EXISTS (SELECT 1 FROM payment_recipients WHERE bank = 'halyk' AND phone = '77029721819');

INSERT INTO payment_recipients (bank, bin, name, phone)
SELECT 'kaspi', '250740003149', 'ТОО КЕЛЕШЕК СПОРТ', NULL
WHERE NOT EXISTS (SELECT 1 FROM payment_recipients WHERE bank = 'kaspi' AND bin = '250740003149');
