"""Apply optional reference/sample data from seeds/*.sql.

Schema migrations are intentionally kept free of environment-specific seed data.
"""

import logging
import os
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

logger = logging.getLogger(__name__)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SEEDS_DIR = os.path.join(_ROOT, "seeds")


def seed() -> None:
    if not config.POSTGRES_DSN:
        logger.warning("POSTGRES_DSN not set - skipping seeds.")
        return
    conn = psycopg2.connect(config.POSTGRES_DSN)
    try:
        for filename in sorted(f for f in os.listdir(_SEEDS_DIR) if f.endswith(".sql")):
            with open(os.path.join(_SEEDS_DIR, filename), encoding="utf-8") as fh:
                sql = fh.read()
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()
            logger.info("Applied seed %s", filename)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    seed()
