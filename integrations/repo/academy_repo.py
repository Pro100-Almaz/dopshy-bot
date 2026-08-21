from datetime import datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool

from integrations.repo.postgres import _conn

'''
groups --> users --> trials
'''

# ----------------------------GROUPS

def create_or_update_group(
    group_name: str,
    group_type: str,
    max_cap: int | None = None,
    is_active: bool = True,
    birth_years: list[int] | None = None,
    location: str | None = None,
    level: str | None = None,
) -> int:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO academy_groups
                    (group_name, group_type, max_cap, is_active, birth_years, location, level)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_name, group_type)
                DO UPDATE SET
                    max_cap = EXCLUDED.max_cap,
                    is_active = EXCLUDED.is_active,
                    birth_years = COALESCE(EXCLUDED.birth_years, academy_groups.birth_years),
                    location = COALESCE(EXCLUDED.location, academy_groups.location),
                    level = COALESCE(EXCLUDED.level, academy_groups.level)
                RETURNING id
                """,
                (group_name, group_type, max_cap, is_active, birth_years, location, level),
            )

            row = cur.fetchone()
            return row["id"]

def on_manual_group_edit(
        group_id: int,
        group_name: str | None = None,
        max_cap : str | None = None,
        level: str | None = None,
) -> dict:
    fields = []
    values = []

    if group_name is not None:
        fields.append("group_name = %s")
        values.append(group_name)
    if max_cap is not None:
        fields.append("max_cap = %s")
        values.append(max_cap)
    if level is not None:
        if level not in {"Beginner", "Intermediate", "Advanced"}:
            return {
                'ok': False,
                'code': 'INVALID_LEVEL',
                'message': 'level must be Beginner, Intermediate, or Advanced'
            }
        fields.append("level = %s")
        values.append(level)

    if not fields:
        return {
            'ok': False,
            "code": "NO_FIELDS",
            "message": "No fields to update"
        }

    values.append(group_id)

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE academy_groups 
                SET {", ".join(fields)},
                updated_at = NOW() 
                WHERE id = %s
                RETURNING id
                """, values,
            )

            row = cur.fetchone()

            if not row:
                return {
                    'ok': False,
                    'code': 'NOT FOUND',
                    'message': 'Group not found'
                }

            return {
                'ok': True,
                'group_id': row['id']
            }


def on_manual_group_schedule_edit(
        group_id: int,
        training_day: int,
        new_training_day: int | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
) -> dict:
    fields = []
    values = []

    if new_training_day is not None:
        fields.append("training_day = %s")
        values.append(new_training_day)
    if time_start is not None:
        fields.append("time_start = %s")
        values.append(time_start)
    if time_end is not None:
        fields.append("time_end = %s")
        values.append(time_end)

    if not fields:
        return {
            'ok': False,
            'code': 'NO_FIELDS',
            'message': 'No schedule fields to update'
        }

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, time_start, time_end
                FROM academy_group_schedules
                WHERE group_id = %s
                  AND training_day = %s
                FOR UPDATE
                """,
                (group_id, training_day)
            )
            rows = cur.fetchall()

            if not rows:
                return {
                    'ok': False,
                    'code': 'SCHEDULE_NOT_FOUND',
                    'message': 'Group schedule not found'
                }

            if len(rows) > 1:
                return {
                    'ok': False,
                    'code': 'AMBIGUOUS_SCHEDULE',
                    'message': 'Multiple schedules found for this group and training day'
                }

            existing = rows[0]
            next_training_day = new_training_day if new_training_day is not None else training_day
            next_start = time_start if time_start is not None else str(existing['time_start'])[:5]
            next_end = time_end if time_end is not None else str(existing['time_end'])[:5]
            if next_training_day < 0 or next_training_day > 6:
                return {
                    'ok': False,
                    'code': 'INVALID_WEEKDAY',
                    'message': 'training_day must be between 0 and 6'
                }
            if datetime.strptime(str(next_start)[:5], "%H:%M") >= datetime.strptime(str(next_end)[:5], "%H:%M"):
                return {
                    'ok': False,
                    'code': 'INVALID_TIME',
                    'message': 'time_start must be before time_end'
                }

            cur.execute(
                """
                SELECT id
                FROM academy_group_schedules
                WHERE group_id = %s
                  AND training_day = %s
                  AND time_start = %s
                  AND time_end = %s
                  AND id <> %s
                """,
                (group_id, next_training_day, next_start, next_end, existing['id'])
            )
            if cur.fetchone():
                return {
                    'ok': False,
                    'code': 'SCHEDULE_CONFLICT',
                    'message': 'A schedule with this weekday and time already exists'
                }

            values.extend([group_id, training_day])
            cur.execute(
                f"""
                UPDATE academy_group_schedules
                SET {", ".join(fields)},
                    updated_at = NOW()
                WHERE group_id = %s
                  AND training_day = %s
                RETURNING id, group_id, training_day, time_start, time_end
                """,
                values,
            )
            row = cur.fetchone()
            return {
                'ok': True,
                'group_id': row['group_id'],
                'schedule_id': row['id'],
                'training_day': row['training_day'],
                'time_start': row['time_start'],
                'time_end': row['time_end'],
            }


def setting_training_time(group_id: int, training_day: int, time_start: str, time_end: str):
    if training_day < 0 or training_day > 6:
        raise ValueError("training_day must be between 0 and 6")
    if datetime.strptime(str(time_start)[:5], "%H:%M") >= datetime.strptime(str(time_end)[:5], "%H:%M"):
        raise ValueError("time_start must be before time_end")
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO academy_group_schedules (group_id, training_day, time_start, time_end)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id, training_day, time_start, time_end) DO UPDATE SET group_id     = EXCLUDED.group_id,
                                                                                         training_day = EXCLUDED.training_day,
                                                                                         time_start   = EXCLUDED.time_start,
                                                                                         time_end     = EXCLUDED.time_end,
                                                                                         updated_at   = NOW()
                    RETURNING id

                """, (group_id, training_day, time_start, time_end,)
            )
            row = cur.fetchone()
            return row["id"] if row else None


def get_groups_info(bot_name: str):
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT s.group_id, s.training_day, s.time_start, s.time_end,
                       g.group_name, g.group_type, g.max_cap, g.curr_cap,
                       g.birth_years, g.location, g.level
                FROM academy_group_schedules s
                JOIN academy_groups g ON g.id = s.group_id
                WHERE g.group_type = %s AND g.is_active = TRUE
                """, (group_type,)
            )
            return [dict(row) for row in cur.fetchall()]


def get_existing_trial_draft(phone: str, bot_name: str) -> dict | None:
    group_type = "boxing" if bot_name == "dopsy_boxing" else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.*
                FROM academy_trials t
                LEFT JOIN academy_groups g ON g.id = t.group_id
                WHERE t.phone = %s
                  AND t.state = 'draft'
                  AND (t.group_id IS NULL OR g.group_type = %s)
                ORDER BY t.updated_at DESC, t.id DESC
                LIMIT 1
                """,
                (phone, group_type),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_groups_for_refresh(group_type: str) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT g.id, g.group_name, g.max_cap, g.curr_cap, g.birth_years, g.location, g.level,
                       s.training_day, s.time_start AS time_start, s.time_end AS time_end
                FROM academy_groups g
                LEFT JOIN academy_group_schedules s
                ON s.group_id = g.id
                WHERE g.group_type = %s
                  AND g.is_active = TRUE
                ORDER BY g.id, s.training_day, s.time_start
                """,
                (group_type,)
            )

            return [dict(row) for row in cur.fetchall()]


def get_all_groups_for_frontend() -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT g.id, g.group_name, g.group_type, g.max_cap, g.curr_cap,
                       g.birth_years, g.location, g.level,
                       s.training_day, s.time_start AS time_start, s.time_end AS time_end
                FROM academy_groups g
                LEFT JOIN academy_group_schedules s
                  ON s.group_id = g.id
                WHERE g.is_active = TRUE
                ORDER BY g.group_type, g.id, s.training_day, s.time_start
                """
            )
            return [dict(row) for row in cur.fetchall()]


def get_group_by_id(group_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM academy_groups WHERE id = %s
                """,
                (group_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def deactivate_group_repo(group_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE academy_groups
                SET is_active = false
                WHERE id = {group_id}
                """,
            )
            return {'ok': '200'}


def get_trial(trial_id: int) -> dict | None:
    """Return a single trial with full detail, or None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                        SELECT *
                        FROM academy_trials
                        WHERE id = %s
                        """, (trial_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def get_trials_by_curriculum(curriculum: str) -> list[dict] | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM academy_trials
                WHERE curriculum = %s
                """, (curriculum,)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]


def get_all_user_trials(user_id: int) -> list[dict] | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.id,
                       t.child_name,
                       t.child_birth_year,
                       t.language,
                       t.phone,
                       t.group_id,
                       t.trial_day,
                       t.start_time,
                       t.end_time,
                       t.state,
                       t.notes,
                       t.attended,
                       t.subscribed
                FROM academy_users u
                JOIN academy_trials t
                  ON t.group_id = u.assigned_group_id
                 AND (
                     t.phone = u.parent_phone
                     OR lower(t.child_name) = lower(u.child_name)
                 )
                WHERE u.id = %s
                ORDER BY t.trial_day, t.start_time, t.id
                """, (user_id,)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]


def get_trials_by_type(group_type: str):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.id,
                       t.child_name,
                       t.child_birth_year,
                       t.language,
                       t.phone,
                       t.group_id,
                       t.trial_day,
                       t.start_time,
                       t.end_time,
                       t.state,
                       t.notes,
                       t.attended,
                       t.subscribed
                FROM academy_trials t
                         JOIN academy_groups g ON t.group_id = g.id
                WHERE g.group_type = %s
                  AND t.state = 'confirmed'

                """, (group_type,)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]


def get_users_by_assigned_group(group_id: int) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id,
                       child_name,
                       child_birth_year,
                       parent_phone,
                       total_trials,
                       assigned_group_id,
                       subscribed
                FROM academy_users
                WHERE assigned_group_id = %s
                ORDER BY child_name, id
                """,
                (group_id,)
            )
            return [dict(row) for row in cur.fetchall()]


def get_user_by_id(user_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id,
                       child_name,
                       child_birth_year,
                       parent_phone,
                       total_trials,
                       assigned_group_id,
                       subscribed
                FROM academy_users
                WHERE id = %s
                """,
                (user_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


_TRIAL_WITH_USER_SELECT = """
    SELECT t.id,
           t.child_name,
           t.child_birth_year,
           t.language,
           t.phone,
           t.group_id,
           t.trial_day,
           t.start_time,
           t.end_time,
           t.state,
           t.notes,
           t.attended,
           t.subscribed,
           u.id AS user_id,
           u.child_name AS user_child_name,
           u.child_birth_year AS user_child_birth_year,
           u.parent_phone AS user_parent_phone,
           u.total_trials AS user_total_trials,
           u.assigned_group_id AS user_assigned_group_id,
           u.subscribed AS user_subscribed
    FROM academy_trials t
    LEFT JOIN LATERAL (
        SELECT au.*
        FROM academy_users au
        WHERE au.assigned_group_id = t.group_id
          AND (
              au.parent_phone = t.phone
              OR lower(au.child_name) = lower(t.child_name)
          )
        ORDER BY
          CASE WHEN au.parent_phone = t.phone THEN 0 ELSE 1 END,
          au.id
        LIMIT 1
    ) u ON TRUE
"""


def get_trials_with_users_by_group(group_id: int) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                _TRIAL_WITH_USER_SELECT + """
                WHERE t.group_id = %s
                  AND t.state = 'confirmed'
                ORDER BY t.trial_day, t.start_time, t.id
                """,
                (group_id,)
            )
            return [dict(row) for row in cur.fetchall()]


def get_trials_with_users_by_type(group_type: str | None = None) -> list[dict]:
    where = ["t.state = 'confirmed'"]
    params: list = []
    if group_type is not None:
        where.append("g.group_type = %s")
        params.append(group_type)

    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                _TRIAL_WITH_USER_SELECT + f"""
                JOIN academy_groups g ON g.id = t.group_id
                WHERE {" AND ".join(where)}
                ORDER BY g.group_type, t.trial_day, t.start_time, t.id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def get_trial_with_user_by_id(trial_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                _TRIAL_WITH_USER_SELECT + """
                WHERE t.id = %s
                """,
                (trial_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_users_by_type(group_type: str | None = None) -> list[dict]:
    where = []
    params: list = []
    if group_type is not None:
        where.append("g.group_type = %s")
        params.append(group_type)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT u.id,
                       u.child_name,
                       u.child_birth_year,
                       u.parent_phone,
                       u.total_trials,
                       u.assigned_group_id,
                       u.subscribed
                FROM academy_users u
                LEFT JOIN academy_groups g ON g.id = u.assigned_group_id
                {where_sql}
                ORDER BY g.group_type, u.child_name, u.id
                """,
                params,
            )
            return [dict(row) for row in cur.fetchall()]


def update_trial_attended(trial_id: int, attended: bool) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE academy_trials
                SET attended = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (attended, trial_id)
            )
            row = cur.fetchone()
            if not row:
                return None

    return get_trial_with_user_by_id(trial_id)


def update_trial_subscribed(trial_id: int, subscribed: bool) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM academy_trials
                WHERE id = %s
                FOR UPDATE
                """,
                (trial_id,)
            )
            trial = cur.fetchone()
            if not trial:
                return None

            cur.execute(
                """
                UPDATE academy_users
                SET subscribed = %s,
                    updated_at = NOW()
                WHERE id = (
                    SELECT au.id
                    FROM academy_users au
                    WHERE au.assigned_group_id = %s
                      AND (
                          au.parent_phone = %s
                          OR lower(au.child_name) = lower(%s)
                      )
                    ORDER BY
                      CASE WHEN au.parent_phone = %s THEN 0 ELSE 1 END,
                      au.id
                    LIMIT 1
                )
                """,
                (
                    subscribed,
                    trial["group_id"],
                    trial["phone"],
                    trial["child_name"],
                    trial["phone"],
                )
            )
            matched_user = cur.rowcount > 0

            if subscribed and not matched_user:
                cur.execute(
                    """
                    INSERT INTO academy_users (
                        child_name,
                        child_birth_year,
                        parent_phone,
                        total_trials,
                        assigned_group_id,
                        subscribed
                    )
                    SELECT %s, %s, %s, COUNT(*), %s, TRUE
                    FROM academy_trials
                    WHERE group_id = %s
                      AND (
                          phone = %s
                          OR lower(child_name) = lower(%s)
                      )
                    """,
                    (
                        trial["child_name"],
                        trial["child_birth_year"],
                        trial["phone"],
                        trial["group_id"],
                        trial["group_id"],
                        trial["phone"],
                        trial["child_name"],
                    )
                )

            cur.execute(
                """
                UPDATE academy_trials
                SET subscribed = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (subscribed, trial_id)
            )
            row = cur.fetchone()
            if not row:
                return None

    return get_trial_with_user_by_id(trial_id)


def update_user_subscribed(user_id: int, subscribed: bool) -> dict | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE academy_users
                SET subscribed = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id,
                          child_name,
                          child_birth_year,
                          parent_phone,
                          total_trials,
                          assigned_group_id,
                          subscribed
                """,
                (subscribed, user_id)
            )
            user = cur.fetchone()
            if not user:
                return None

            cur.execute(
                """
                UPDATE academy_trials
                SET subscribed = %s,
                    updated_at = NOW()
                WHERE group_id = %s
                  AND (
                      phone = %s
                      OR lower(child_name) = lower(%s)
                  )
                """,
                (
                    subscribed,
                    user["assigned_group_id"],
                    user["parent_phone"],
                    user["child_name"],
                )
            )
            return dict(user)


def confirm_trial(trial_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM academy_trials WHERE id = %s FOR UPDATE", (trial_id,))
            trial = cur.fetchone()
            if not trial:
                return False

            cur.execute(
                """
                INSERT INTO academy_users
                    (child_name, child_birth_year, parent_phone, total_trials,
                     assigned_group_id, experience, school_shift)
                VALUES (%s, %s, %s, 1, %s, %s, %s)
                ON CONFLICT (parent_phone, child_name, assigned_group_id)
                DO UPDATE SET
                    child_birth_year = EXCLUDED.child_birth_year,
                    experience = EXCLUDED.experience,
                    school_shift = EXCLUDED.school_shift,
                    total_trials = academy_users.total_trials + 1,
                    updated_at = NOW()
                RETURNING id
                """,
                (trial["child_name"], trial["child_birth_year"], trial["phone"],
                 trial["group_id"], trial["experience"], trial["school_shift"]),
            )
            user_id = cur.fetchone()["id"]
            cur.execute(
                """UPDATE academy_trials
                   SET state = 'confirmed', user_id = %s
                   WHERE id = %s""", (user_id, trial_id)
            )
            return True


def get_all_active_trials(sender_phone: str, bot_name: str) -> list[dict] | None:
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.*
                FROM academy_trials t
                LEFT JOIN academy_groups g ON g.id = t.group_id
                WHERE t.state IN ('confirmed', 'draft')
                  AND t.phone = %s
                  AND (t.group_id IS NULL OR g.group_type = %s)
                ORDER BY t.trial_day NULLS LAST, t.start_time NULLS LAST, t.id
                """,
                (sender_phone, group_type),
            )
            trials = cur.fetchall()
            return [dict(t) for t in trials]


def cancel_all_trials(trial_ids: list) -> None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""DELETE FROM academy_trials WHERE id = ANY(%s) and state IN ('draft', 'confirmed')""", (trial_ids,)
            )


def check_trial_limits(bot_name: str, phone: str) -> bool:
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                        SELECT COUNT(*) < (SELECT quantity
                                           FROM trial_limits
                                           WHERE group_type = %s)
                                   AS can_take_trial
                        FROM academy_trials at
                                 JOIN academy_groups ag ON ag.id = at.group_id
                        WHERE at.phone = %s
                          AND ag.group_type = %s
                        """, (group_type, phone, group_type))

            can_take_trial = cur.fetchone()["can_take_trial"]
            return can_take_trial


def has_active_trial(bot_name: str, phone: str) -> bool:
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                        SELECT EXISTS (SELECT 1
                                       FROM academy_trials at
                                                JOIN academy_groups ag ON ag.id = at.group_id
                                       WHERE at.phone = %s
                                         AND ag.group_type = %s
                                         AND at.state = 'confirmed')
                        """, (phone, group_type))

            has_confirmed_trial = cur.fetchone()
            return has_confirmed_trial['exists']
