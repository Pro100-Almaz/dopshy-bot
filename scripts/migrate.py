"""Lightweight SQL migration runner.

Applies migrations/*.sql in filename order, tracking applied files in a
schema_migrations table. After SQL migrations, seeds the `fields` reference
table from config.BOOKING_FIELDS. Idempotent and safe to run on every startup.

Usage:
    poetry run python scripts/migrate.py
"""

import logging
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402

logger = logging.getLogger(__name__)

_MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


def _applied(cur) -> set[str]:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  filename   TEXT PRIMARY KEY,"
        "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
        ")"
    )
    cur.execute("SELECT filename FROM schema_migrations")
    return {r[0] for r in cur.fetchall()}


def _strip_sql_comments(sql: str) -> str:
    """Remove /* ... */ blocks and -- line comments."""
    out, i, n = [], 0, len(sql)
    while i < n:
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            i = n if end == -1 else end + 1
        else:
            out.append(sql[i])
            i += 1
    return "".join(out)


def _has_statement(sql: str) -> bool:
    """False when a file holds nothing but comments and whitespace.

    psycopg2 raises "can't execute an empty query" on such a file, which aborts
    the whole run — and since app.py migrates at startup, that means a fresh
    deployment cannot boot. It happens legitimately: when reference data is
    moved out of a migration into seeds/, the migration is left as a
    comment-only record of the change (see 014_add_keleshek_sport_bin.sql).
    """
    return bool(_strip_sql_comments(sql).strip())


def _seed_fields(cur) -> None:
    for f in config.BOOKING_FIELDS:
        price = 45000 if f["format"] == "6x6" else 35000
        cur.execute(
            "INSERT INTO fields (id, name, format, price_per_hour) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (f["id"], f"Поле {f['id']} ({f['format']})", f["format"], price),
        )


def migrate() -> None:
    """Apply all pending migrations and seed reference data."""
    if not config.POSTGRES_DSN:
        logger.warning("POSTGRES_DSN not set — skipping migrations.")
        return

    files = sorted(
        f for f in os.listdir(_MIGRATIONS_DIR) if f.endswith(".sql")
    )

    conn = psycopg2.connect(config.POSTGRES_DSN)
    try:
        with conn.cursor() as cur:
            done = _applied(cur)
        conn.commit()

        for filename in files:
            if filename in done:
                continue
            path = os.path.join(_MIGRATIONS_DIR, filename)
            with open(path, encoding="utf-8") as fh:
                sql = fh.read()
            executable = _has_statement(sql)
            with conn.cursor() as cur:
                if executable:
                    cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)",
                    (filename,),
                )
            conn.commit()
            if executable:
                logger.info("Applied migration %s", filename)
            else:
                logger.info(
                    "Migration %s has no statements — recorded as applied", filename
                )

        with conn.cursor() as cur:
            _seed_fields(cur)
        conn.commit()
        logger.info("Migrations up to date (%d files).", len(files))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    migrate()
