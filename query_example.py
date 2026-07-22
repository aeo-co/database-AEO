from db import get_conn


def get_client_trend(client_id: int, weeks: int = 8):
    """
    Pull the last N weeks of GA4 + Shopify numbers side by side for one
    client - a simple example of the "cross data" / historical trend
    queries agents will run once more sources are flowing in.

    Add more LEFT JOINs (gsc_weekly, ai_visibility_weekly, ...) the same
    way as more sources come online.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    g.week_start,
                    g.sessions,
                    g.conversions,
                    s.orders,
                    s.revenue
                FROM ga4_weekly g
                LEFT JOIN shopify_weekly s
                    ON s.client_id = g.client_id AND s.week_start = g.week_start
                WHERE g.client_id = %(client_id)s
                ORDER BY g.week_start DESC
                LIMIT %(weeks)s;
                """,
                {"client_id": client_id, "weeks": weeks},
            )
            return cur.fetchall()


if __name__ == "__main__":
    rows = get_client_trend(client_id=1)
    if not rows:
        print("No rows yet - run migrate.py, seed.py, then ingest_example.py first.")
    for row in rows:
        print(row)
