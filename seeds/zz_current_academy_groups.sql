-- Current academy group schedule.
--
-- This seed no longer deletes academy data. It marks groups not present in this
-- file inactive, then upserts the current groups and schedules below.

CREATE TEMP TABLE _seed_academy_groups (
    row_no INTEGER PRIMARY KEY,
    group_name TEXT NOT NULL,
    group_type TEXT NOT NULL,
    is_active BOOLEAN NOT NULL,
    max_cap INTEGER NOT NULL,
    birth_years INTEGER[],
    location TEXT,
    level TEXT[],
    trainer TEXT
) ON COMMIT DROP;

CREATE TEMP TABLE _seed_academy_schedules (
    row_no INTEGER NOT NULL,
    training_day INTEGER NOT NULL,
    time_start TIME NOT NULL,
    time_end TIME NOT NULL,
    field INTEGER
) ON COMMIT DROP;

INSERT INTO _seed_academy_groups (
    row_no, group_name, group_type, is_active, max_cap,
    birth_years, location, level, trainer
)
VALUES
    (1,  'Special group 2014-2012', 'football', TRUE, 20, ARRAY[2014, 2013, 2012], NULL, ARRAY['Advanced'], NULL),
    (2,  'Special group 2016-2017', 'football', TRUE, 20, ARRAY[2016, 2017],       NULL, ARRAY['Advanced'], NULL),
    (3,  'Special group 2018-2019', 'football', TRUE, 20, ARRAY[2018, 2019],       NULL, ARRAY['Advanced'], NULL),
    (4,  'Group 2015-2014',         'football', TRUE, 20, ARRAY[2015, 2014],       NULL, ARRAY['Advanced'], NULL),
    (5,  'Group 2017-2016',         'football', TRUE, 20, ARRAY[2017, 2016],       NULL, ARRAY['Advanced'], NULL),
    (6,  'Group 2016-2017',         'football', TRUE, 20, ARRAY[2016, 2017],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (7,  'Group 2018-2019',         'football', TRUE, 20, ARRAY[2018, 2019],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (8,  'Group 2019-2020',         'football', TRUE, 20, ARRAY[2019, 2020],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (9,  'Group 2020-2021',         'football', TRUE, 20, ARRAY[2020, 2021],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (10, 'Group 2013-2015',         'football', TRUE, 20, ARRAY[2013, 2014, 2015], NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (11, 'Group 2014-2015',         'football', TRUE, 20, ARRAY[2014, 2015],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (12, 'Group 2017-2018',         'football', TRUE, 20, ARRAY[2017, 2018],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (13, 'Group 2012-2013',         'football', TRUE, 20, ARRAY[2012, 2013],       NULL, ARRAY['Beginner', 'Intermediate'], NULL),
    (14, 'Group 2022',              'football', TRUE, 20, ARRAY[2022],             NULL, ARRAY['Beginner', 'Intermediate'], NULL),

    -- Summer boxing groups are preserved as inactive historical schedule data.
    (101, 'Box Timur Summer 10:00',  'boxing', FALSE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (102, 'Box Timur Summer 19:00',  'boxing', FALSE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (103, 'Box Askhat Summer 10:00', 'boxing', FALSE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Адильханов Асхат Адильханович'),
    (104, 'Box Askhat Summer 16:00', 'boxing', FALSE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Адильханов Асхат Адильханович'),

    -- Current boxing groups. Names intentionally do not include a month so the
    -- frontend and bot do not expose seasonal labels after the schedule changes.
    (201, 'Box Timur 09:00',         'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (202, 'Box Timur 10:00',         'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (203, 'Box Timur 17:00',         'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (204, 'Box Timur 18:00',         'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (205, 'Box Timur 19:30',         'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Бейсенов Тимур'),
    (206, 'Box Askhat 10:00',        'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Адильханов Асхат Адильханович'),
    (207, 'Box Askhat 16:00',        'boxing', TRUE, 25, ARRAY[2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019, 2020], 'second floor', ARRAY['Beginner', 'Intermediate', 'Advanced'], 'Адильханов Асхат Адильханович');

-- Weekdays: Monday=0, Tuesday=1, Wednesday=2, Thursday=3, Friday=4, Saturday=5.
INSERT INTO _seed_academy_schedules (row_no, training_day, time_start, time_end, field)
VALUES
    (1, 0, '08:15', '09:30', NULL), (1, 2, '08:15', '09:30', NULL), (1, 4, '08:15', '09:30', NULL),
    (2, 0, '09:30', '10:45', NULL), (2, 2, '09:30', '10:45', NULL), (2, 4, '09:30', '10:45', NULL),
    (3, 0, '10:45', '12:00', NULL), (3, 2, '10:45', '12:00', NULL), (3, 4, '10:45', '12:00', NULL),
    (4, 1, '09:30', '10:45', NULL), (4, 3, '09:30', '10:45', NULL), (4, 5, '09:30', '10:45', NULL),
    (5, 1, '09:30', '10:45', NULL), (5, 3, '09:30', '10:45', NULL), (5, 5, '09:30', '10:45', NULL),
    (6, 1, '16:00', '17:15', 3), (6, 3, '16:00', '17:15', 3), (6, 5, '09:30', '10:45', 3),
    (7, 1, '16:00', '17:15', 2), (7, 3, '16:00', '17:15', 2), (7, 5, '09:30', '10:45', 2),
    (8, 1, '16:00', '17:15', 1), (8, 3, '16:00', '17:15', 1), (8, 5, '09:30', '10:45', 1),
    (9, 1, '17:15', '18:30', 3), (9, 3, '17:15', '18:30', 3), (9, 5, '10:45', '12:00', 3),
    (10, 1, '17:15', '18:30', 3), (10, 3, '17:15', '18:30', 3), (10, 5, '09:30', '10:45', 3),
    (11, 0, '16:00', '17:15', 2), (11, 2, '16:00', '17:15', 2), (11, 4, '16:00', '17:15', 2),
    (12, 0, '16:00', '17:15', 3), (12, 2, '16:00', '17:15', 3), (12, 4, '16:00', '17:15', 3),
    (13, 0, '17:15', '18:30', 1), (13, 2, '17:15', '18:30', 1), (13, 4, '17:15', '18:30', 1),
    (14, 0, '17:15', '18:30', 3), (14, 2, '17:15', '18:30', 3), (14, 4, '17:15', '18:30', 3),

    (101, 1, '10:00', '11:00', NULL), (101, 3, '10:00', '11:00', NULL), (101, 5, '10:00', '11:00', NULL),
    (102, 1, '19:00', '20:00', NULL), (102, 3, '19:00', '20:00', NULL), (102, 5, '10:00', '11:00', NULL),
    (103, 0, '10:00', '11:00', NULL), (103, 2, '10:00', '11:00', NULL), (103, 4, '10:00', '11:00', NULL),
    (104, 0, '16:00', '17:00', NULL), (104, 2, '16:00', '17:00', NULL), (104, 4, '16:00', '17:00', NULL),

    (201, 1, '09:00', '10:00', NULL), (201, 3, '09:00', '10:00', NULL), (201, 5, '09:00', '10:00', NULL),
    (202, 1, '10:00', '11:00', NULL), (202, 3, '10:00', '11:00', NULL), (202, 5, '10:00', '11:00', NULL),
    (203, 1, '17:00', '18:00', NULL), (203, 3, '17:00', '18:00', NULL), (203, 5, '17:00', '18:00', NULL),
    (204, 1, '18:00', '19:00', NULL), (204, 3, '18:00', '19:00', NULL), (204, 5, '18:00', '19:00', NULL),
    (205, 1, '19:30', '20:30', NULL), (205, 3, '19:30', '20:30', NULL), (205, 5, '19:30', '20:30', NULL),
    (206, 0, '10:00', '11:00', NULL), (206, 2, '10:00', '11:00', NULL), (206, 4, '10:00', '11:00', NULL),
    (207, 0, '16:00', '17:00', NULL), (207, 2, '16:00', '17:00', NULL), (207, 4, '16:00', '17:00', NULL);

UPDATE academy_groups g
SET is_active = FALSE,
    updated_at = NOW()
WHERE g.group_type IN ('football', 'boxing')
  AND NOT EXISTS (
      SELECT 1
      FROM _seed_academy_groups sg
      WHERE sg.group_name = g.group_name
        AND sg.group_type = g.group_type
  );

WITH inserted AS (
    INSERT INTO academy_groups (
        group_name, group_type, max_cap, is_active,
        birth_years, location, level, trainer
    )
    SELECT
        group_name,
        group_type,
        max_cap,
        is_active,
        birth_years,
        location,
        level,
        trainer
    FROM _seed_academy_groups
    ORDER BY row_no
    ON CONFLICT (group_name, group_type)
    DO UPDATE SET
        max_cap = EXCLUDED.max_cap,
        is_active = EXCLUDED.is_active,
        birth_years = EXCLUDED.birth_years,
        location = EXCLUDED.location,
        level = EXCLUDED.level,
        trainer = EXCLUDED.trainer,
        updated_at = NOW()
    RETURNING id, group_name, group_type
)
INSERT INTO academy_group_schedules (group_id, training_day, time_start, time_end, field)
SELECT i.id, s.training_day, s.time_start, s.time_end, s.field
FROM inserted i
JOIN _seed_academy_groups g
  ON g.group_name = i.group_name
 AND g.group_type = i.group_type
JOIN _seed_academy_schedules s ON s.row_no = g.row_no
ORDER BY g.row_no, s.training_day, s.time_start
ON CONFLICT (group_id, training_day, time_start, time_end)
DO UPDATE SET
    field = EXCLUDED.field,
    updated_at = NOW();
