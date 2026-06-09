import psycopg2
import psycopg2.extras
import psycopg2.pool

from integrations.repo.postgres import _conn

'''
groups --> users --> trials
'''


# ----------------------------GROUPS


def create_group(group_name: str, group_type: str, max_cap: int | None = None, ):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO academy_groups (group_name, group_type, max_cap)
                VALUES (%s, %s, %s)
                RETURNING id
                """, (group_name, group_type, max_cap)
            )


def setting_training_time(group_id: int, date: str, time_start: str, time_end: str):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO academy_group_schedules (group_id, training_date, start_time, end_time)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (group_id, training_day, time_start, time_end) DO UPDATE SET group_id     = EXCLUDED.group_id,
                                                                                         training_day = EXCLUDED.training_date,
                                                                                         time_start   = EXCLUDED.time_start,
                                                                                         time_end     = EXCLUDED.time_end,
                                                                                         updated_at   = NOW()

                """, (group_id, date, time_start, time_end,)
            )


def get_groups_info(bot_name: str):
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT group_id, training_day, time_start, time_end FROM academy_group_schedules 
                WHERE group_id IN (SELECT id FROM academy_groups WHERE group_type = %s AND is_active = TRUE) 
                """, (group_type,)
            )
            return [dict(row) for row in cur.fetchall()]


def get_groups_for_refresh(group_type: str) -> list[dict]:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT g.id,
                       g.group_name,
                       g.max_cap,
                       g.curr_cap,
                       t.training_day,
                       t.time_start AS start_time,
                       t.time_end   AS end_time
                FROM academy_groups g
                         JOIN academy_trials t
                              ON t.group_id = g.id
                WHERE g.group_type = %s
                  AND g.is_active = TRUE
                ORDER BY g.id, t.training_day, t.time_start
                """,
                (group_type,)
            )

            return [dict(row) for row in cur.fetchall()]


def get_group_by_id(group_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM academy_groups
                WHERE id = group_id
                """,
            )
            return dict(cur.fetchone())


def deactivate_group_repo(group_id: int) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE academy_groups
                SET is_active = false
                WHERE id = %s
                """, group_id
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
                """, (curriculum)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]


def get_all_user_trials(user_id: int) -> list[dict] | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT *
                FROM academy_trials
                WHERE user_id = %s
                """, (user_id)
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
                       t.child_age,
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


def confirm_trial(trial_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """UPDATE academy_trials
                   SET state = 'confirmed'
                   WHERE id = %s""", (trial_id,)
            )
            return True


def get_all_active_trials(sender_phone: str, bot_name: str) -> list[dict] | None:
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""SELECT * FROM academy_trials 
                WHERE state IN ('confirmed', 'draft') AND phone = '{sender_phone}' 
                AND group_id IN (SELECT id FROM academy_groups WHERE group_type = '{group_type}')"""
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

