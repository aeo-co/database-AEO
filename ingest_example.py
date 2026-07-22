from datetime import date

from psycopg.types.json import Jsonb

from db import get_conn


def upsert_ga4_week(
    client_id: int,
    week_start: date,
    week_end: date,
    sessions: int,
    users: int,
    conversions: int,
    bounce_rate: float,
    extra_metrics: dict | None = None,
):
    """
    Insert or update one client's GA4 numbers for a given week.
    Safe to run more than once for the same client/week - it overwrites
    rather than duplicating, so a re-run of a weekly job won't create
    dupe rows.

    Run this once per client per week, e.g. from a scheduled job that
    pulls from the GA4 API and calls this with the results.

    This exact pattern (a fixed set of well-known columns + a JSONB
    "metrics" catch-all for anything else) is the one to copy for
    GSC, Shopify, AI visibility, or any other weekly source - swap the
    table name and column list, same shape.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ga4_weekly
                    (client_id, week_start, week_end, sessions, users,
                     conversions, bounce_rate, metrics)
                VALUES
                    (%(client_id)s, %(week_start)s, %(week_end)s, %(sessions)s,
                     %(users)s, %(conversions)s, %(bounce_rate)s, %(metrics)s)
                ON CONFLICT (client_id, week_start) DO UPDATE SET
                    week_end = EXCLUDED.week_end,
                    sessions = EXCLUDED.sessions,
                    users = EXCLUDED.users,
                    conversions = EXCLUDED.conversions,
                    bounce_rate = EXCLUDED.bounce_rate,
                    metrics = EXCLUDED.metrics,
                    ingested_at = now();
                """,
                {
                    "client_id": client_id,
                    "week_start": week_start,
                    "week_end": week_end,
                    "sessions": sessions,
                    "users": users,
                    "conversions": conversions,
                    "bounce_rate": bounce_rate,
                    "metrics": Jsonb(extra_metrics or {}),
                },
            )


if __name__ == "__main__":
    # Stand-in for a real GA4 API pull - replace this call's inputs with
    # whatever the actual API response gives you once that's wired up.
    upsert_ga4_week(
        client_id=1,
        week_start=date(2026, 7, 6),
        week_end=date(2026, 7, 12),
        sessions=4820,
        users=3510,
        conversions=112,
        bounce_rate=42.3,
        extra_metrics={"new_users": 2100, "avg_session_duration_sec": 134},
    )
    print("Inserted example GA4 row for client_id=1, week of 2026-07-06.")
