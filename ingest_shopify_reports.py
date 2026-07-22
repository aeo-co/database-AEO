"""
Ingestion for the multi-section client report CSVs (the "-all-data.csv"
files). These aren't plain flat CSVs - each file has several sections
marked with "=== Section Name ===" headers, and the columns inside each
section vary from client to client (even sections with the same name
don't always have the same columns). So this stores each section
generically - whatever columns and rows it actually has, as plain text,
no type coercion - rather than assuming one fixed shape.

Point this at a folder (or list files) named like:
    {client}-all-data.csv

Usage:
    python ingest_shopify_reports.py /path/to/folder
    python ingest_shopify_reports.py file1.csv file2.csv
"""
import csv
import io
import re
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from db import get_conn
from ingest_ai_visibility import slugify

FILENAME_RE = re.compile(r"^(?P<client>.+?)-all-data\.csv$", re.IGNORECASE)
SECTION_RE = re.compile(r"^===\s*(.+?)\s*===$")


def get_or_create_client(cur, name: str) -> int:
    slug = slugify(name)
    cur.execute(
        """
        INSERT INTO clients (name, slug) VALUES (%s, %s)
        ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug
        RETURNING id;
        """,
        (name.strip(), slug),
    )
    return cur.fetchone()["id"]


def parse_sections(text: str) -> dict:
    """Split the raw file into {section_name: [line, line, ...]} chunks."""
    sections = {}
    current = None
    for line in text.splitlines():
        m = SECTION_RE.match(line.strip())
        if m:
            # Strip a leading "01 " style number - it's just this file's
            # ordering, not a stable id across clients.
            name = re.sub(r"^\d+\s+", "", m.group(1)).strip()
            current = name
            sections[current] = []
            continue
        if current is not None and line.strip():
            sections[current].append(line)
    return sections


def parse_csv_lines(lines: list) -> tuple:
    """Turn a section's raw lines into (columns, rows), handling quoted
    commas properly (e.g. "6,600") via the real csv module rather than a
    naive split on ','."""
    reader = csv.reader(io.StringIO("\n".join(lines)))
    parsed = [row for row in reader if row]
    if not parsed:
        return [], []
    return parsed[0], parsed[1:]


def ingest_file(path: Path) -> dict:
    m = FILENAME_RE.match(path.name)
    if not m:
        return {
            "filename": path.name,
            "status": "skipped",
            "reason": "name doesn't match '{client}-all-data.csv'",
        }

    client_name = m.group("client").replace("-", " ").replace("_", " ").strip().title()
    text = path.read_text(encoding="utf-8-sig")
    sections = parse_sections(text)

    if not sections:
        return {"filename": path.name, "status": "skipped", "reason": "no '=== section ===' markers found"}

    with get_conn() as conn:
        with conn.cursor() as cur:
            client_id = get_or_create_client(cur, client_name)
            for section_name, lines in sections.items():
                columns, rows = parse_csv_lines(lines)
                cur.execute(
                    """
                    INSERT INTO shopify_report_sections (client_id, section_name, columns, rows, source_file)
                    VALUES (%(client_id)s, %(section_name)s, %(columns)s, %(rows)s, %(source_file)s)
                    ON CONFLICT (client_id, section_name) DO UPDATE SET
                        columns = EXCLUDED.columns,
                        rows = EXCLUDED.rows,
                        source_file = EXCLUDED.source_file,
                        ingested_at = now();
                    """,
                    {
                        "client_id": client_id,
                        "section_name": section_name,
                        "columns": Jsonb(columns),
                        "rows": Jsonb(rows),
                        "source_file": path.name,
                    },
                )

    return {
        "filename": path.name,
        "status": "ok",
        "client": client_name,
        "client_slug": slugify(client_name),
        "sections": len(sections),
    }


def main(inputs: list[str]):
    files = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(p.glob("*.csv")))
        elif p.suffix.lower() == ".csv":
            files.append(p)

    if not files:
        print("No .csv files found.")
        return

    print(f"Found {len(files)} file(s):")
    for f in files:
        result = ingest_file(f)
        if result["status"] == "skipped":
            print(f"  SKIP {result['filename']} - {result['reason']}")
        else:
            print(f"  {result['filename']} -> client='{result['client']}' sections={result['sections']}")
    print("Done.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["."])
