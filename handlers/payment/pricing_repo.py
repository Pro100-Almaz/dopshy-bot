import psycopg2.extras

from integrations.repo.utils import _conn


def get_total_field_prices() -> list:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT ff.format_name, ff.pricing_type, ff.price_per_hour FROM field_prices AS ff"
            )
            return [dict(p) for p in cur.fetchall()]


def get_prices_for_format(format_name: str) -> dict:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT pricing_type, price_per_hour FROM field_prices WHERE format_name = %s",
                (format_name,),
            )
            return {row["pricing_type"]: float(row["price_per_hour"]) for row in cur.fetchall()}
