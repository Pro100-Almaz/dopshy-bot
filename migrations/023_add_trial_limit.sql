CREATE TABLE IF NOT EXISTS trial_limits (
    id SERIAL PRIMARY KEY,
    quantity INTEGER,
    group_type VARCHAR(30)
);