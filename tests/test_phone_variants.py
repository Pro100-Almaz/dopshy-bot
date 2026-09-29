"""Phones are saved digits-only, but lookups must match every spelling.

Regression: trial drafts were saved as '77072479672' while the flow looked them
up with the raw WhatsApp '+77072479672', so every message created a new draft
and the intake never got past its first two questions.
"""
import uuid

import pytest

from integrations.repo import academy_repo, bot_pause_repo
from integrations.repo import postgres as svc
from integrations.repo.postgres import _conn
from integrations.repo.utils import phone_variants

_RAW = "+77010000123"
_DIGITS = "77010000123"


@pytest.mark.no_db
def test_phone_variants_covers_raw_digits_and_plus_forms():
    assert phone_variants("+7 701 000-01-23") == ["+7 701 000-01-23", _DIGITS, _RAW]
    assert phone_variants(_DIGITS) == [_DIGITS, _RAW]
    assert phone_variants(_RAW) == [_RAW, _DIGITS]
    assert phone_variants(None) == []
    assert phone_variants("") == []


@pytest.fixture
def clean_trials():
    def wipe():
        with _conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM academy_trials WHERE phone = ANY(%s)",
                            (phone_variants(_DIGITS),))
                cur.execute("DELETE FROM bot_paused_contacts WHERE phone = ANY(%s)",
                            (phone_variants(_DIGITS),))
    wipe()
    yield
    wipe()


def test_draft_saved_normalized_is_found_by_raw_whatsapp_phone(clean_trials):
    created = svc.create_draft("dopsy_boxing", chat_id="chat-phone", phone=_RAW,
                               client_token=str(uuid.uuid4()))
    trial_id = created["data"]["trial_id"]

    stored = academy_repo.get_trial(trial_id)
    assert stored["phone"] == _DIGITS

    for spelling in (_RAW, _DIGITS):
        draft = academy_repo.get_existing_trial_draft(spelling, "dopsy_boxing")
        assert draft and draft["id"] == trial_id, spelling


def test_legacy_plus_prefixed_row_is_still_found(clean_trials):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO academy_trials (phone, client_token, state) "
                "VALUES (%s, %s, 'draft') RETURNING id",
                (_RAW, str(uuid.uuid4())),
            )
            legacy_id = cur.fetchone()[0]

    draft = academy_repo.get_existing_trial_draft(_DIGITS, "dopsy_boxing")
    assert draft and draft["id"] == legacy_id
    assert [t["id"] for t in academy_repo.get_all_active_trials(_DIGITS, "dopsy_boxing")] == [legacy_id]


def test_pause_state_matches_legacy_and_normalized_rows(clean_trials):
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO bot_paused_contacts (phone, paused, reason) VALUES (%s, TRUE, 'manual')",
                (_RAW,),
            )

    assert bot_pause_repo.is_bot_paused(_DIGITS)
    assert bot_pause_repo.get_statuses([_DIGITS])[_DIGITS]["paused"] is True

    bot_pause_repo.set_bot_paused(_RAW, False)   # writes the normalized row
    assert not bot_pause_repo.is_bot_paused(_RAW)
    assert bot_pause_repo.get_statuses([_RAW])[_RAW]["paused"] is False
