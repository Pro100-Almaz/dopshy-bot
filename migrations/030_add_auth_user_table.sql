CREATE TABLE auth_users (
    id BIGSERIAL PRIMARY KEY,

    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,

    phone_number VARCHAR(15) UNIQUE,

    role VARCHAR(20) NOT NULL DEFAULT 'manager',

    first_name VARCHAR(100),
    last_name VARCHAR(100),

    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);