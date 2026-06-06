import psycopg2
import psycopg2.extras
import psycopg2.pool

import json
import config
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

                """, (date, time_start, time_end,)
            )


def get_group_info(bot_name: str):
    group_type = "boxing" if bot_name == 'dopsy_boxing' else "football"
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT group_id, training_day, time_start, time_end FROM academy_group_schedules 
                WHERE group_id IN (SELECT id FROM academy_groups WHERE group_type = %s) 
                """, (group_type,)
            )
            return [dict(row) for row in cur.fetchall()]


def deactivate_group_repo(group_id: int):
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE academy_groups
                SET is_active = false
                WHERE id = %s
                """, group_id
            )


def get_trial(trial_id : int) -> dict | None:
    """Return a single trial with full detail, or None."""
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT * FROM academy_trials WHERE id = %s
            """, (trial_id))
            row = cur.fetchone()
            return dict(row) if row else None


def get_trials_by_curriculum(curriculum: str) -> list[dict] | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM academy_trials WHERE curriculum = %s
                """, (curriculum)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]


def get_all_user_trials(user_id: int) -> list[dict] | None:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM academy_trials WHERE user_id = %s
                """, (user_id)
            )
            trials = cur.fetchall()
            return [dict(trial) for trial in trials]





def get_available_groups():
    pass



def upsert_trial():
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                    
                """
            )



def cancel_trial():
    pass
