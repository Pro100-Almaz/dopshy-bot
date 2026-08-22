-- Current academy group schedule.
--
-- This seed intentionally clears previous academy group/trial/user data and
-- replaces it with the provided football academy schedule.
TRUNCATE trial_sessions, academy_trials, academy_users,
         academy_group_schedules, academy_groups
RESTART IDENTITY CASCADE;

CREATE TEMP TABLE _seed_academy_groups (
    row_no INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    birth_years INTEGER[] NOT NULL,
    location TEXT,
    level TEXT[] NOT NULL,
    field INTEGER
) ON COMMIT DROP;

CREATE TEMP TABLE _seed_academy_schedules (
    row_no INTEGER NOT NULL,
    training_day INTEGER NOT NULL,
    time_start TIME NOT NULL,
    time_end TIME NOT NULL
) ON COMMIT DROP;

INSERT INTO _seed_academy_groups (row_no, group_name, birth_years, location, level, field)
VALUES
    (1,  'Special group 2014-2012', ARRAY[2014, 2013, 2012], NULL, ARRAY['Advanced'], NULL),
    (2,  'Special group 2016-2017', ARRAY[2016, 2017],       NULL, ARRAY['Advanced'], NULL),
    (3,  'Special group 2018-2019', ARRAY[2018, 2019],       NULL, ARRAY['Advanced'], NULL),
    (4,  'Group 2015-2014',         ARRAY[2015, 2014],       NULL, ARRAY['Advanced'], NULL),
    (5,  'Group 2017-2016',         ARRAY[2017, 2016],       NULL, ARRAY['Advanced'], NULL),
    (6,  'Group 2016-2017', ARRAY[2016, 2017],       NULL, ARRAY['Beginner', 'Intermediate'], 3),
    (7,  'Group 2018-2019', ARRAY[2018, 2019],       NULL, ARRAY['Beginner', 'Intermediate'], 2),
    (8,  'Group 2019-2020', ARRAY[2019, 2020],       NULL, ARRAY['Beginner', 'Intermediate'], 1),
    (9,  'Group 2020-2021', ARRAY[2020, 2021],       NULL, ARRAY['Beginner', 'Intermediate'], 3),
    (10, 'Group 2013-2015', ARRAY[2013, 2014, 2015], NULL, ARRAY['Beginner', 'Intermediate'], 3),
    (11, 'Group 2014-2015', ARRAY[2014, 2015],       NULL, ARRAY['Beginner', 'Intermediate'], 2),
    (12, 'Group 2017-2018', ARRAY[2017, 2018],       NULL, ARRAY['Beginner', 'Intermediate'], 3),
    (13, 'Group 2012-2013', ARRAY[2012, 2013],       NULL, ARRAY['Beginner', 'Intermediate'], 1),
    (14, 'Group 2022',      ARRAY[2022],             NULL, ARRAY['Beginner', 'Intermediate'], 3);

-- Weekdays: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5.
-- End times are inferred as 75-minute sessions from the provided start-time grid.
INSERT INTO _seed_academy_schedules (row_no, training_day, time_start, time_end)
VALUES
    (1, 0, '08:15', '09:30'), (1, 2, '08:15', '09:30'), (1, 4, '08:15', '09:30'),
    (2, 0, '09:30', '10:45'), (2, 2, '09:30', '10:45'), (2, 4, '09:30', '10:45'),
    (3, 0, '10:45', '12:00'), (3, 2, '10:45', '12:00'), (3, 4, '10:45', '12:00'),
    (4, 1, '09:30', '10:45'), (4, 3, '09:30', '10:45'), (4, 5, '09:30', '10:45'),
    (5, 1, '09:30', '10:45'), (5, 3, '09:30', '10:45'), (5, 5, '09:30', '10:45'),
    (6, 1, '16:00', '17:15'), (6, 3, '16:00', '17:15'), (6, 5, '09:30', '10:45'),
    (7, 1, '16:00', '17:15'), (7, 3, '16:00', '17:15'), (7, 5, '09:30', '10:45'),
    (8, 1, '16:00', '17:15'), (8, 3, '16:00', '17:15'), (8, 5, '09:30', '10:45'),
    (9, 1, '17:15', '18:30'), (9, 3, '17:15', '18:30'), (9, 5, '10:45', '12:00'),
    (10, 1, '17:15', '18:30'), (10, 3, '17:15', '18:30'), (10, 5, '09:30', '10:45'),
    (11, 0, '16:00', '17:15'), (11, 2, '16:00', '17:15'), (11, 4, '16:00', '17:15'),
    (12, 0, '16:00', '17:15'), (12, 2, '16:00', '17:15'), (12, 4, '16:00', '17:15'),
    (13, 0, '17:15', '18:30'), (13, 2, '17:15', '18:30'), (13, 4, '17:15', '18:30'),
    (14, 0, '17:15', '18:30'), (14, 2, '17:15', '18:30'), (14, 4, '17:15', '18:30');

WITH inserted AS (
    INSERT INTO academy_groups (
        group_name, group_type, max_cap, curr_cap, is_active,
        birth_years, location, level
    )
    SELECT
        group_name,
        'football',
        20,
        0,
        TRUE,
        birth_years,
        location,
        level
    FROM _seed_academy_groups
    ORDER BY row_no
    RETURNING id, group_name
)
INSERT INTO academy_group_schedules (group_id, training_day, time_start, time_end, field)
SELECT i.id, s.training_day, s.time_start, s.time_end, g.field
FROM inserted i
JOIN _seed_academy_groups g ON g.group_name = i.group_name
JOIN _seed_academy_schedules s ON s.row_no = g.row_no
ORDER BY g.row_no, s.training_day, s.time_start;
