import sys

from db import get_conn


def platform_summary(client_slug: str):
    """
    Average visibility score and brand position per platform, for one
    client - shows whether a brand shows up better on Perplexity vs GPT
    vs Google AI Mode, the kind of cross-platform read the boss wants
    out of a single source of truth.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    v.platform,
                    count(*) AS queries_tested,
                    round(avg(v.visibility_score), 1) AS avg_visibility_score,
                    round(avg(v.brand_position), 1) AS avg_brand_position
                FROM ai_visibility_checks v
                JOIN clients c ON c.id = v.client_id
                WHERE c.slug = %(slug)s
                GROUP BY v.platform
                ORDER BY v.platform;
                """,
                {"slug": client_slug},
            )
            return cur.fetchall()


if __name__ == "__main__":
    slug = sys.argv[1] if len(sys.argv) > 1 else "altenew"
    rows = platform_summary(slug)
    if not rows:
        print(f"No rows for client slug '{slug}' - run ingest_ai_visibility.py first.")
    for row in rows:
        print(row)
