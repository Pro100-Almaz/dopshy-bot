"""Guards for the migration runner's handling of statement-free files.

A migration left holding only comments (which happens when its reference data is
moved out to seeds/, as in 014_add_keleshek_sport_bin.sql) made psycopg2 raise
"can't execute an empty query" and aborted the whole run. app.py migrates at
startup, so that stopped a fresh deployment from booting — and it made
tests/conftest.py skip every DB test with "test database unreachable".
"""

import os

from scripts.migrate import _has_statement, _strip_sql_comments

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def test_comment_only_file_has_no_statement():
    assert not _has_statement("-- just a note\n-- and another\n")


def test_block_comment_only_file_has_no_statement():
    assert not _has_statement("/* a note\n   spanning lines */\n\n")


def test_empty_and_whitespace_files_have_no_statement():
    assert not _has_statement("")
    assert not _has_statement("\n\n   \t\n")


def test_real_statement_is_detected():
    assert _has_statement("-- a note\nALTER TABLE t ADD COLUMN c INTEGER;\n")
    assert _has_statement("CREATE TABLE t (id SERIAL);")


def test_quotes_inside_comments_do_not_look_like_statements():
    # 014's header contains ТОО "КЕЛЕШЕК СПОРТ" — a naive quote-based check
    # would wrongly treat that as executable SQL.
    assert not _has_statement('-- Add the ТОО "КЕЛЕШЕК СПОРТ" seller BIN\n')


def test_trailing_comment_after_statement_still_counts():
    assert _has_statement("ALTER TABLE t ADD COLUMN c INT; -- trailing note\n")


def test_strip_leaves_statement_text():
    stripped = _strip_sql_comments("-- note\nSELECT 1; /* inline */ SELECT 2;\n")
    assert "SELECT 1;" in stripped
    assert "SELECT 2;" in stripped
    assert "note" not in stripped
    assert "inline" not in stripped


def test_unterminated_comment_markers_do_not_crash():
    assert not _has_statement("/* never closed\n")
    assert not _has_statement("-- no trailing newline")


def test_every_migration_file_is_readable_and_classifiable():
    """Every migration must either carry a statement or be a known no-op.

    A file that silently becomes statement-free is now recorded rather than
    executed, so this asserts the set of no-ops stays deliberate.
    """
    expected_no_ops = {"014_add_keleshek_sport_bin.sql"}
    found_no_ops = set()

    files = [f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith(".sql")]
    assert files, "no migration files found"

    for filename in sorted(files):
        with open(os.path.join(_MIGRATIONS_DIR, filename), encoding="utf-8") as fh:
            if not _has_statement(fh.read()):
                found_no_ops.add(filename)

    assert found_no_ops == expected_no_ops, (
        f"statement-free migrations changed: {found_no_ops} != {expected_no_ops}"
    )
