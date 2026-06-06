CREATE TABLE IF NOT EXISTS academy_groups (
    id SERIAL PRIMARY KEY,
    group_name VARCHAR(40) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    max_cap INTEGER DEFAULT 20,
    curr_cap INTEGER DEFAULT 0,
    group_type VARCHAR(20),
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW()

);


CREATE TABLE IF NOT EXISTS academy_group_schedules (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES academy_groups(id) ON DELETE CASCADE,
    training_day INTEGER NOT NULL CHECK (TRAINING_DAY BETWEEN 0 AND 6),
    time_start TIME NOT NULL,
    time_end TIME NOT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    CHECK (time_start < time_end),
    UNIQUE (group_id, training_day, time_start, time_end)
);


CREATE TABLE IF NOT EXISTS academy_users (
    id  SERIAL PRIMARY KEY,
    child_name VARCHAR(40) NOT NULL,
    child_age INTEGER,
    child_birth_date DATE,
    parent_phone VARCHAR(15),
    total_trials INTEGER DEFAULT 0,
    assigned_group_id BIGINT DEFAULT NULL,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    FOREIGN KEY (assigned_group_id) REFERENCES academy_groups(id)
);


CREATE TABLE IF NOT EXISTS academy_trials (
    id  SERIAL PRIMARY KEY,
    language VARCHAR(10),
    state VARCHAR(30),

    group_id BIGINT,
    user_id BIGINT,

    trial_day DATE NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,

    attended BOOLEAN DEFAULT FALSE,
    subscribed BOOLEAN DEFAULT FALSE,
    notes TEXT,

    created_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT NOW(),

    FOREIGN KEY (user_id) REFERENCES academy_users(id),
    FOREIGN KEY (group_id) REFERENCES academy_groups(id)
);

CREATE TABLE IF NOT EXISTS trial_sessions (
    chat_id    TEXT        PRIMARY KEY,
    state      VARCHAR(20) NOT NULL,
    params     JSONB       NOT NULL DEFAULT '{}',
    trial_id INTEGER     REFERENCES academy_trials(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
