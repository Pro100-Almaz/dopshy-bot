-- Per-field hourly price (used to compute the required payment amount).

ALTER TABLE fields ADD COLUMN IF NOT EXISTS price_per_hour NUMERIC(10, 2);

UPDATE fields
SET price_per_hour = CASE format WHEN '6x6' THEN 45000 ELSE 35000 END
WHERE price_per_hour IS NULL;
