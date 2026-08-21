WITH seeded_groups AS (
    INSERT INTO academy_groups (group_name, group_type, max_cap, curr_cap, is_active)
    VALUES
        ('Boxing Juniors A', 'boxing', 16, 3, TRUE),
        ('Football Juniors A', 'football', 18, 2, TRUE)
    ON CONFLICT (group_name, group_type)
    DO UPDATE SET
        max_cap = EXCLUDED.max_cap,
        curr_cap = EXCLUDED.curr_cap,
        is_active = TRUE,
        updated_at = NOW()
    RETURNING id, group_name, group_type
),
target_groups AS (
    SELECT id, group_name, group_type
    FROM seeded_groups
    UNION
    SELECT id, group_name, group_type
    FROM academy_groups
    WHERE (group_name, group_type) IN (
        ('Boxing Juniors A', 'boxing'),
        ('Football Juniors A', 'football')
    )
),
seeded_schedules AS (
    INSERT INTO academy_group_schedules (group_id, training_day, time_start, time_end)
    SELECT g.id, s.training_day, s.time_start::time, s.time_end::time
    FROM target_groups g
    JOIN (
        VALUES
            ('Boxing Juniors A', 'boxing', 1, '17:00', '18:30'),
            ('Boxing Juniors A', 'boxing', 3, '17:00', '18:30'),
            ('Football Juniors A', 'football', 2, '18:00', '19:30'),
            ('Football Juniors A', 'football', 4, '18:00', '19:30')
    ) AS s(group_name, group_type, training_day, time_start, time_end)
      ON s.group_name = g.group_name
     AND s.group_type = g.group_type
    ON CONFLICT (group_id, training_day, time_start, time_end)
    DO UPDATE SET updated_at = NOW()
    RETURNING id
),
seed_users AS (
    SELECT *
    FROM (
        VALUES
            ('Boxing Juniors A', 'boxing', 'Ayan Karimov', 2017, '+77010001001', 1, FALSE),
            ('Boxing Juniors A', 'boxing', 'Miras Sadykov', 2016, '+77010001002', 2, TRUE),
            ('Boxing Juniors A', 'boxing', 'Timur Akhmetov', 2018, '+77010001003', 0, FALSE),
            ('Football Juniors A', 'football', 'Dias Omarov', 2017, '+77010002001', 1, TRUE),
            ('Football Juniors A', 'football', 'Arman Ibrayev', 2015, '+77010002002', 0, FALSE)
    ) AS u(group_name, group_type, child_name, child_birth_year, parent_phone, total_trials, subscribed)
),
seeded_users AS (
    INSERT INTO academy_users (
        child_name,
        child_birth_year,
        parent_phone,
        total_trials,
        assigned_group_id,
        subscribed
    )
    SELECT
        u.child_name,
        u.child_birth_year,
        u.parent_phone,
        u.total_trials,
        g.id,
        u.subscribed
    FROM seed_users u
    JOIN target_groups g
      ON g.group_name = u.group_name
     AND g.group_type = u.group_type
    WHERE NOT EXISTS (
        SELECT 1
        FROM academy_users existing
        WHERE existing.assigned_group_id = g.id
          AND existing.parent_phone = u.parent_phone
          AND lower(existing.child_name) = lower(u.child_name)
    )
    RETURNING id
)
INSERT INTO academy_trials (
    language,
    state,
    group_id,
    trial_day,
    start_time,
    end_time,
    attended,
    subscribed,
    notes,
    phone,
    child_name,
    child_birth_year
)
SELECT
    t.language,
    'confirmed',
    g.id,
    t.trial_day,
    t.start_time::time,
    t.end_time::time,
    t.attended,
    t.subscribed,
    t.notes,
    t.phone,
    t.child_name,
    t.child_birth_year
FROM (
    VALUES
        ('Boxing Juniors A', 'boxing', 'ru', DATE '2026-08-17', '17:00', '18:30', TRUE, TRUE, 'Seed trial: attended and subscribed.', '+77010001002', 'Miras Sadykov', 10),
        ('Boxing Juniors A', 'boxing', 'kz', DATE '2026-08-19', '17:00', '18:30', FALSE, FALSE, 'Seed trial: upcoming.', '+77010001001', 'Ayan Karimov', 9),
        ('Football Juniors A', 'football', 'ru', DATE '2026-08-18', '18:00', '19:30', TRUE, TRUE, 'Seed trial: attended and subscribed.', '+77010002001', 'Dias Omarov', 9)
) AS t(group_name, group_type, language, trial_day, start_time, end_time, attended, subscribed, notes, phone, child_name, child_birth_year)
JOIN target_groups g
  ON g.group_name = t.group_name
 AND g.group_type = t.group_type
WHERE NOT EXISTS (
    SELECT 1
    FROM academy_trials existing
    WHERE existing.group_id = g.id
      AND existing.phone = t.phone
      AND lower(existing.child_name) = lower(t.child_name)
      AND existing.trial_day = t.trial_day
      AND existing.start_time = t.start_time::time
);

WITH seeded_groups AS (
    INSERT INTO academy_groups (group_name, group_type, max_cap, curr_cap, is_active)
    VALUES
        ('Boxing Beginners B', 'boxing', 14, 0, TRUE),
        ('Boxing Teens Elite', 'boxing', 12, 0, TRUE),
        ('Boxing Morning Kids', 'boxing', 10, 0, TRUE),
        ('Football Beginners B', 'football', 16, 0, TRUE),
        ('Football Teens Elite', 'football', 14, 0, TRUE),
        ('Football Morning Kids', 'football', 12, 0, TRUE)
    ON CONFLICT (group_name, group_type)
    DO UPDATE SET
        max_cap = EXCLUDED.max_cap,
        curr_cap = EXCLUDED.curr_cap,
        is_active = TRUE,
        updated_at = NOW()
    RETURNING id, group_name, group_type
),
target_groups AS (
    SELECT id, group_name, group_type
    FROM seeded_groups
    UNION
    SELECT id, group_name, group_type
    FROM academy_groups
    WHERE (group_name, group_type) IN (
        ('Boxing Beginners B', 'boxing'),
        ('Boxing Teens Elite', 'boxing'),
        ('Boxing Morning Kids', 'boxing'),
        ('Football Beginners B', 'football'),
        ('Football Teens Elite', 'football'),
        ('Football Morning Kids', 'football')
    )
)
INSERT INTO academy_group_schedules (group_id, training_day, time_start, time_end)
SELECT g.id, s.training_day, s.time_start::time, s.time_end::time
FROM target_groups g
JOIN (
    VALUES
        ('Boxing Beginners B', 'boxing', 0, '16:00', '17:30'),
        ('Boxing Beginners B', 'boxing', 2, '16:00', '17:30'),
        ('Boxing Teens Elite', 'boxing', 1, '19:00', '20:30'),
        ('Boxing Teens Elite', 'boxing', 4, '19:00', '20:30'),
        ('Boxing Morning Kids', 'boxing', 5, '10:00', '11:30'),
        ('Football Beginners B', 'football', 1, '16:30', '18:00'),
        ('Football Beginners B', 'football', 3, '16:30', '18:00'),
        ('Football Teens Elite', 'football', 2, '19:30', '21:00'),
        ('Football Teens Elite', 'football', 5, '19:30', '21:00'),
        ('Football Morning Kids', 'football', 6, '10:30', '12:00')
) AS s(group_name, group_type, training_day, time_start, time_end)
  ON s.group_name = g.group_name
 AND s.group_type = g.group_type
ON CONFLICT (group_id, training_day, time_start, time_end)
DO UPDATE SET updated_at = NOW();
