INSERT INTO ycloud_enabled (id, bot_name, is_enabled)
VALUES
    (1, 'arena', TRUE),
    (2, 'football_academy', TRUE),
    (3, 'boxing_academy', TRUE)
ON CONFLICT (bot_name) DO NOTHING;
