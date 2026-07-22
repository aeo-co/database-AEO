"""
Dynamic ingestion for AI visibility tool exports.

Point this at a folder (or list individual files) containing exports named:

    {Client} - {Platform} - {YYYY-MM-DD}.xlsx

e.g. "Altenew - AI Mode - 2026-03-17.xlsx". Underscore-separated names
also work ("Altenew_-_AI_Mode_-_2026-03-17.xlsx") - spacing style doesn't
matter. .csv works the same as .xlsx, and an extra trailing bit after the
date (like "- Results", which some CSV exports add - it's just the sheet
name tacked on) is fine too. New clients and new platforms are picked up
automatically from the filename - nothing to register or configure by
hand before uploading a new client's files.

Usage:
    python ingest_ai_visibility.py /path/to/folder
    python ingest_ai_visibility.py file1.xlsx file2.csv
"""
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd
from psycopg.types.json import Jsonb

from db import get_conn

FILENAME_RE = re.compile(
    r"^(?P<client>.+?)[\s_]*-[\s_]*(?P<platform>.+?)[\s_]*-[\s_]*(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:[\s_]*-[\s_]*.*?)?\.(?:xlsx|csv)$",
    re.IGNORECASE,
)
# Just a date in the filename, anywhere before the extension. Used by
# the multi-platform path when the full regex doesn't match (file is
# named like "Client - YYYY-MM-DD.xlsx" with no platform slot).
DATE_IN_NAME_RE = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})",
)

# Known platform name -> canonical slug. Anything not listed here still
# works - it just falls back to a slugified version of whatever string
# shows up in the filename, so a brand-new platform doesn't break this.
PLATFORM_ALIASES = {
    "ai mode": "google_ai_mode",
    "gpt": "chatgpt",
    "chatgpt": "chatgpt",
    "perplexity": "perplexity",
}

# Columns the export is expected to have, after stripping whitespace from
# headers. "Mention N" columns are handled separately since the count and
# exact spacing varies (Mention 1 vs Mention2 vs Mention 10).
EXPECTED_COLUMNS = {
    "Date", "Brand", "Query", "Raw Output", "URLS",
    "Visbility score", "Total Brands", "Brand Postions",
    "Competitors Analysis", "Sources", "Queries",
}


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")


def normalize_platform(raw: str) -> str:
    key = raw.replace("_", " ").strip().lower()
    return PLATFORM_ALIASES.get(key, slugify(key))


def clean_str(val):
    if val is None:
        return None
    s = str(val)
    return None if s.strip().lower() == "nan" else s


def clean_num(val):
    try:
        f = float(val)
        return None if f != f else f  # NaN != NaN
    except (TypeError, ValueError):
        return None


def parse_json_cell(val) -> list:
    if not isinstance(val, str):
        return []
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return []


def collect_mentions(row: dict) -> list:
    mention_cols = sorted(
        (c for c in row if re.match(r"(?i)^mention\s*\d+\s*$", c)),
        key=lambda c: int(re.search(r"\d+", c).group()),
    )
    return [row[c].strip() for c in mention_cols if isinstance(row[c], str) and row[c].strip()]


def get_or_create_client(cur, name: str) -> int:
    name = name.strip()
    slug = slugify(name)
    # First, see if either the slug or the (case-insensitive) name already
    # exists - the ON CONFLICT below only catches slug collisions, but the
    # name column is also unique, so an existing client with a different
    # slug (e.g. "Outdoorvitals" vs "outdoor-vitals") would still hit a
    # unique-violation on insert. Resolve to the existing row instead.
    cur.execute(
        "SELECT id FROM clients "
        "WHERE slug = %s OR lower(name) = lower(%s) "
        "LIMIT 1;",
        (slug, name),
    )
    row = cur.fetchone()
    if row:
        return row["id"]
    cur.execute(
        "INSERT INTO clients (name, slug) VALUES (%s, %s) RETURNING id;",
        (name, slug),
    )
    return cur.fetchone()["id"]


def ingest_file(path: Path) -> dict:
    m = FILENAME_RE.match(path.name)
    platform_from_name = None
    client_from_name = None
    date_from_name = None
    if m:
        client_from_name = m.group("client").replace("_", " ").strip()
        platform_from_name = normalize_platform(m.group("platform"))
        date_from_name = pd.to_datetime(m.group("date")).date()
    else:
        # Pull just the date out of the filename for the multi-platform
        # path, which doesn't need a platform slot.
        d = DATE_IN_NAME_RE.search(path.stem)
        if d:
            date_from_name = pd.to_datetime(d.group("date")).date()

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        df = pd.read_excel(path, sheet_name=0)
    df.columns = [c.strip() for c in df.columns]

    # Two accepted file shapes:
    #   1. Filename matches "Client - Platform - YYYY-MM-DD[.xlsx|.csv]" -
    #      one file per platform. Original contract.
    #   2. Newer exports bundle all platforms in one file with a Platform
    #      column. Group by the column; pull date from the filename,
    #      client from the filename or the Brand column.
    # If a Platform column is present, that wins - the file is a bundled
    # multi-platform export regardless of what the filename looks like.
    if "Platform" in df.columns:
        brand_value = next(
            (str(v).strip() for v in df["Brand"] if pd.notna(v)
             and str(v).strip().lower() != "nan"),
            None,
        )
        client_name = client_from_name or brand_value or ""
        if not client_name or date_from_name is None:
            return {
                "filename": path.name,
                "status": "skipped",
                "reason": ("multi-platform xlsx needs a YYYY-MM-DD in the "
                           "filename and either a client name or a Brand "
                           "column in the file"),
            }
        platform = None  # set per-group below
        date_iter = lambda _: date_from_name
    elif platform_from_name is not None:
        # Legacy: single-platform file named "Client - Platform - Date"
        missing = EXPECTED_COLUMNS - set(df.columns)
        mention_cols = [c for c in df.columns
                        if re.match(r"(?i)^mention\s*\d+\s*$", c)]
        if missing or not mention_cols:
            return {
                "filename": path.name,
                "status": "skipped",
                "reason": (f"columns don't match what's expected "
                           f"(missing: {sorted(missing) or 'none'})"),
            }
        client_name = client_from_name
        platform = platform_from_name
        date_iter = lambda _: pd.to_datetime(_["Date"]).date()
    else:
        return {
            "filename": path.name,
            "status": "skipped",
            "reason": ("name doesn't match "
                       "'Client - Platform - YYYY-MM-DD[.xlsx|.csv]' "
                       "and no Platform column in the file"),
        }

    written = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client_name)

            def insert_row(row):
                nonlocal written
                query_text = clean_str(row.get("Query"))
                if not query_text:
                    return
                check_date = date_iter(row)
                query_hash = hashlib.md5(
                    query_text.strip().lower().encode()
                ).hexdigest()
                cur.execute(
                    """
                    INSERT INTO ai_visibility_checks (
                        client_id, platform, check_date, query_text, query_hash,
                        raw_output, urls, mentions, visibility_score, total_brands,
                        brand_position, competitor_analysis, sources, related_queries,
                        source_file
                    ) VALUES (
                        %(client_id)s, %(platform)s, %(check_date)s, %(query_text)s, %(query_hash)s,
                        %(raw_output)s, %(urls)s, %(mentions)s, %(visibility_score)s, %(total_brands)s,
                        %(brand_position)s, %(competitor_analysis)s, %(sources)s, %(related_queries)s,
                        %(source_file)s
                    )
                    ON CONFLICT (client_id, platform, check_date, query_hash) DO UPDATE SET
                        raw_output = EXCLUDED.raw_output,
                        urls = EXCLUDED.urls,
                        mentions = EXCLUDED.mentions,
                        visibility_score = EXCLUDED.visibility_score,
                        total_brands = EXCLUDED.total_brands,
                        brand_position = EXCLUDED.brand_position,
                        competitor_analysis = EXCLUDED.competitor_analysis,
                        sources = EXCLUDED.sources,
                        related_queries = EXCLUDED.related_queries,
                        source_file = EXCLUDED.source_file,
                        ingested_at = now();
                    """,
                    {
                        "client_id": client_id,
                        "platform": platform,
                        "check_date": check_date,
                        "query_text": query_text,
                        "query_hash": query_hash,
                        "raw_output": clean_str(row.get("Raw Output")),
                        "urls": Jsonb(parse_json_cell(row.get("URLS"))),
                        "mentions": Jsonb(collect_mentions(row)),
                        "visibility_score": clean_num(row.get("Visbility score")),
                        "total_brands": clean_num(row.get("Total Brands")),
                        "brand_position": clean_num(row.get("Brand Postions")),
                        "competitor_analysis": clean_str(row.get("Competitors Analysis")),
                        "sources": Jsonb(parse_json_cell(row.get("Sources"))),
                        "related_queries": Jsonb(parse_json_cell(row.get("Queries"))),
                        "source_file": path.name,
                    },
                )
                written += 1

            if platform is not None:
                # Path 1 - single platform from filename
                for _, series in df.iterrows():
                    insert_row(series.to_dict())
            else:
                # Path 2 - one row per platform group
                for raw_platform, group in df.groupby("Platform"):
                    platform = normalize_platform(str(raw_platform))
                    for _, series in group.iterrows():
                        insert_row(series.to_dict())

    return {
        "filename": path.name,
        "status": "ok",
        "client": client_name,
        "client_slug": slugify(client_name),
        "platform": platform or ",".join(
            sorted(normalize_platform(str(p)) for p in df["Platform"].unique())
        ),
        "rows": written,
    }


def main(inputs: list[str]):
    files = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.xlsx")) + sorted(p.glob("*.csv")))
        elif p.suffix.lower() in (".xlsx", ".csv"):
            files.append(p)

    if not files:
        print("No .xlsx or .csv files found.")
        return

    print(f"Found {len(files)} file(s):")
    total = 0
    for f in files:
        result = ingest_file(f)
        if result["status"] == "skipped":
            print(f"  SKIP {result['filename']} - {result['reason']}")
        else:
            print(f"  {result['filename']} -> client='{result['client']}' platform='{result['platform']}' rows={result['rows']}")
            total += result["rows"]
    print(f"Done - {total} rows written across {len(files)} file(s).")


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
