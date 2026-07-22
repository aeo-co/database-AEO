"""
Terminal lookup - no web UI. Type a client's name, get both reports
printed straight to the terminal.

Usage:
    python client_report.py altenew
    python client_report.py "outdoor vitals"
"""
import sys

from tabulate import tabulate

from db import get_conn
from ingest_ai_visibility import slugify


def get_client(query: str):
    slug = slugify(query)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, slug FROM clients WHERE slug = %(slug)s;", {"slug": slug})
            return cur.fetchone()


def print_ai_visibility(client_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT platform,
                       count(*) AS queries,
                       round(avg(visibility_score), 1) AS avg_score,
                       round(avg(brand_position), 1) AS avg_position
                FROM ai_visibility_checks
                WHERE client_id = %(client_id)s
                GROUP BY platform ORDER BY platform;
                """,
                {"client_id": client_id},
            )
            summary_rows = cur.fetchall()

            cur.execute(
                """
                SELECT platform, check_date, query_text, visibility_score,
                       brand_position, total_brands, mentions, raw_output,
                       competitor_analysis
                FROM ai_visibility_checks
                WHERE client_id = %(client_id)s
                ORDER BY check_date DESC, platform;
                """,
                {"client_id": client_id},
            )
            detail_rows = cur.fetchall()

    print("\nAI VISIBILITY")
    print("-" * 60)
    if not summary_rows:
        print("(none yet)")
        return

    table = [[r["platform"], r["queries"], r["avg_score"], r["avg_position"]] for r in summary_rows]
    print(tabulate(table, headers=["Platform", "Queries", "Avg score", "Avg position"]))

    for i, r in enumerate(detail_rows, 1):
        print(f"\n[{i}] {r['platform']} | {r['check_date']} | score: {r['visibility_score']} | "
              f"position: {r['brand_position'] if r['brand_position'] is not None else '—'} of {r['total_brands'] or '—'}")
        print(f"Query: {r['query_text']}")
        if r["mentions"]:
            print(f"Mentioned: {', '.join(r['mentions'])}")
        if r["competitor_analysis"]:
            print(f"\nCompetitor analysis:\n{r['competitor_analysis']}")
        if r["raw_output"]:
            print(f"\nRaw response:\n{r['raw_output']}")
        print("-" * 60)


def print_shopify_report(client_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT section_name, columns, rows
                FROM shopify_report_sections
                WHERE client_id = %(client_id)s
                ORDER BY id;
                """,
                {"client_id": client_id},
            )
            sections = cur.fetchall()

    print("\nSHOPIFY REPORT")
    print("-" * 60)
    if not sections:
        print("(none yet)")
        return
    for s in sections:
        print(f"\n{s['section_name']}")
        print(tabulate(s["rows"], headers=s["columns"]))


def main():
    if len(sys.argv) < 2:
        print('Usage: python client_report.py "<client name>"')
        return

    query = " ".join(sys.argv[1:])
    client = get_client(query)
    if not client:
        print(f"No client matching '{query}'.")
        return

    print("=" * 60)
    print(client["name"])
    print("=" * 60)
    print_ai_visibility(client["id"])
    print_shopify_report(client["id"])


if __name__ == "__main__":
    main()
