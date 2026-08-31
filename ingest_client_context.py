"""
Ingest one markdown file as client brand context.

Usage:
    python ingest_client_context.py <file.md> --client <slug|name|id> \
        --type brand_voice [--title "..."] [--metadata '{"lang":"en"}']

Client can be the id, the slug, or a name fragment — same resolution
the MCP server uses. Embedding provider/key comes from the environment:
EMBEDDING_API_KEY (or OPENAI_API_KEY), model via EMBEDDING_MODEL
(default text-embedding-3-small, 1536-dim — must match the vector
column dimension in migrate_client_contexts.sql).

Requires the pgvector package:  pip install pgvector openai
"""
import argparse
import json
import sys
from pathlib import Path

from client_context import upsert_client_context


def resolve_client_id(client: str) -> int:
    """Accept '3', 'altenew', or a name fragment like 'outdoor vitals'."""
    if client.isdigit():
        return int(client)
    from db import get_conn
    from ingest_ai_visibility import slugify
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM clients WHERE slug = %s;", (slugify(client),))
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute(
                "SELECT id FROM clients "
                "WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') LIKE %s LIMIT 1;",
                (f"%{client.strip().lower()}%",),
            )
            row = cur.fetchone()
    if not row:
        sys.exit(f"error: no client matching '{client}' — check `list_clients` on the MCP server")
    return row["id"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="markdown file to ingest")
    ap.add_argument("--client", required=True, help="client id, slug, or name fragment")
    ap.add_argument("--type", required=True, dest="context_type",
                    help="context_type, e.g. brand_voice | icp | journey_map | case_study")
    ap.add_argument("--title", default=None, help="optional title (part of the upsert key)")
    ap.add_argument("--metadata", default="{}", help="JSON object for the metadata column")
    args = ap.parse_args()

    p = Path(args.file)
    if not p.exists():
        sys.exit(f"error: file not found: {p}")
    if p.suffix.lower() not in (".md", ".markdown", ".txt"):
        sys.exit(f"error: expected a markdown file, got: {p.name}")

    try:
        metadata = json.loads(args.metadata)
    except json.JSONDecodeError as e:
        sys.exit(f"error: --metadata is not valid JSON: {e}")

    client_id = resolve_client_id(args.client)
    result = upsert_client_context(
        client_id=client_id,
        context_type=args.context_type,
        content=p.read_text(encoding="utf-8"),
        title=args.title,
        metadata=metadata,
        source_file=p.name,
    )
    action = "inserted" if result["inserted"] else "REPLACED existing row"
    print(f"OK: {action} client_contexts id={result['id']} "
          f"(client_id={client_id}, type={args.context_type}, title={args.title})")


if __name__ == "__main__":
    main()
