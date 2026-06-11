import psycopg2.extras

from integrations.repo.postgres import _conn

def get_total_field_prices() -> list:
    with _conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT ff.format_name, ff.pricing_type, ff.price_per_hour FROM field_prices AS ff
                """
            )
            prices = [dict(p) for p in cur.fetchall()]
            return prices
