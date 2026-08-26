"""The two gates that decide whether a phone may start a new academy trial.

Both were letting clients into a dead end:

  has_active_trial()   had no date bound, so one confirmed trial blocked the
                       number forever — every later attempt died on
                       HAS_ACTIVE_TRIAL before a draft was created, leaving the
                       client with no new row and no way forward.

  check_trial_limits() counted every row for the phone regardless of state, so
                       cancelled trials, edit churn and abandoned drafts all
                       consumed quota permanently.

These tests own their rows: conftest's clean_db truncates only the booking
tables, not the academy ones.
"""

from datetime import timedelta

import pytest

from integrations.repo.academy_repo import check_trial_limits, has_active_trial
from integrations.repo.utils import _conn
from utils import today_almaty

BOXING_BOT = "dopsy_boxing"
FOOTBALL_BOT = "dopsy_fs_school"
PHONE = "+77000000001"


def _reset():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM academy_trials WHERE phone LIKE '+7700000%'")
            cur.execute("DELETE FROM trial_limits")


def _group(cur, group_type):
    cur.execute(
        "SELECT id FROM academy_groups WHERE group_type = %s LIMIT 1", (group_type,)
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        "INSERT INTO academy_groups (group_name, group_type) VALUES (%s, %s) RETURNING id",
        (f"test-{group_type}", group_type),
    )
    return cur.fetchone()[0]


def _add(state, attended=False, day_offset=None, group_type="boxing", phone=PHONE):
    """Insert one trial row. day_offset is days from today (None -> NULL day)."""
    day = None if day_offset is None else today_almaty() + timedelta(days=day_offset)
    with _conn() as conn:
        with conn.cursor() as cur:
            gid = _group(cur, group_type)
            cur.execute(
                "INSERT INTO academy_trials (phone, state, attended, group_id, "
                "trial_day, start_time, end_time) VALUES (%s,%s,%s,%s,%s,'10:00','11:00')",
                (phone, state, attended, gid, day),
            )


def _set_limit(quantity, group_type="boxing"):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trial_limits (quantity, group_type) VALUES (%s, %s)",
                (quantity, group_type),
            )


@pytest.fixture(autouse=True)
def _clean_academy():
    _reset()
    yield
    _reset()


# ---------------------------------------------------------------- has_active_trial


def test_no_rows_is_not_blocked():
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_upcoming_confirmed_trial_blocks():
    _add("confirmed", day_offset=5)
    assert has_active_trial(BOXING_BOT, PHONE) is True


def test_trial_today_still_blocks():
    """A trial happening today has not happened yet — it must still block."""
    _add("confirmed", day_offset=0)
    assert has_active_trial(BOXING_BOT, PHONE) is True


def test_past_confirmed_trial_does_not_block():
    """The deadloop: a trial that already happened locked the number forever."""
    _add("confirmed", day_offset=-60)
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_past_attended_trial_does_not_block():
    _add("confirmed", attended=True, day_offset=-60)
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_several_past_trials_do_not_block():
    _add("confirmed", attended=True, day_offset=-90)
    _add("confirmed", day_offset=-30)
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_past_and_upcoming_together_still_blocks():
    _add("confirmed", attended=True, day_offset=-60)
    _add("confirmed", day_offset=3)
    assert has_active_trial(BOXING_BOT, PHONE) is True


def test_cancelled_upcoming_trial_does_not_block():
    _add("cancelled", day_offset=5)
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_draft_does_not_block():
    _add("draft", day_offset=5)
    assert has_active_trial(BOXING_BOT, PHONE) is False


def test_other_group_type_does_not_block():
    """A football trial must not block a boxing signup."""
    _add("confirmed", day_offset=5, group_type="football")
    assert has_active_trial(BOXING_BOT, PHONE) is False
    assert has_active_trial(FOOTBALL_BOT, PHONE) is True


# ------------------------------------------------------------- check_trial_limits


def test_no_limit_row_means_unlimited():
    """Stage has an empty trial_limits, so the gate must never fire there."""
    _add("confirmed", attended=True, day_offset=-10)
    _add("confirmed", attended=True, day_offset=-20)
    assert check_trial_limits(BOXING_BOT, PHONE) is True


def test_attended_trial_consumes_quota():
    _set_limit(1)
    _add("confirmed", attended=True, day_offset=-10)
    assert check_trial_limits(BOXING_BOT, PHONE) is False


def test_confirmed_but_not_attended_does_not_consume_quota():
    _set_limit(1)
    _add("confirmed", attended=False, day_offset=-10)
    assert check_trial_limits(BOXING_BOT, PHONE) is True


def test_cancelled_trials_do_not_consume_quota():
    _set_limit(1)
    for _ in range(3):
        _add("cancelled", day_offset=-10)
    assert check_trial_limits(BOXING_BOT, PHONE) is True


def test_abandoned_draft_does_not_consume_quota():
    _set_limit(1)
    _add("draft", day_offset=-10)
    assert check_trial_limits(BOXING_BOT, PHONE) is True


def test_edit_churn_does_not_consume_quota():
    """Each client edit cancels and recreates — it must not cost a trial."""
    _set_limit(1)
    _add("cancelled", day_offset=-10)
    _add("cancelled", day_offset=-9)
    _add("draft", day_offset=-8)
    assert check_trial_limits(BOXING_BOT, PHONE) is True


def test_limit_counts_only_attended_among_mixed_rows():
    _set_limit(2)
    _add("confirmed", attended=True, day_offset=-10)
    _add("cancelled", day_offset=-9)
    _add("confirmed", attended=False, day_offset=-8)
    assert check_trial_limits(BOXING_BOT, PHONE) is True

    _add("confirmed", attended=True, day_offset=-7)
    assert check_trial_limits(BOXING_BOT, PHONE) is False
